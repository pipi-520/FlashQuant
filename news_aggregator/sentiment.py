"""中英文金融情绪打分（可插拔后端，与量化策略共用）。

后端：
- lexicon : 离线词典（默认、零依赖、永远可用）
- finbert : FinBERT 系列（transformers，首次评分时惰性加载，失败回退词典）
- llm     : OpenAI 兼容 chat/completions（结构化 JSON 输出，失败回退词典）

统一入口：
- score_text(text)      -> [-1, 1]，动态路由到当前后端
- score_batch(texts)    -> list[float]
- configure_backend(cfg) 读取 config['sentiment']['backend'] 切换后端

旧接口兼容：CN_POS / CN_NEG / score_text 仍可正常 import。
"""

import json
import os
import re

# ---------- 中文 ----------

CN_POS = ["利好", "增长", "上涨", "涨停", "突破", "超预期", "回购", "增持", "中标",
          "盈利", "扭亏", "创新高", "获批", "签约", "分红", "业绩预增", "预增", "降准", "降息",
          "刺激", "支持", "扩产", "提价", "涨价", "翻倍", "大涨", "回升", "回暖", "改善", "走强"]
CN_NEG = ["利空", "下跌", "跌停", "亏损", "减持", "违规", "处罚", "立案", "调查",
          "退市", "风险", "爆雷", "商誉减值", "质押", "冻结", "诉讼", "下调", "不及预期",
          "预亏", "预减", "停产", "召回", "违约", "债务", "暴跌", "走弱", "承压", "下挫",
          "下滑", "下降", "回落", "低于预期"]

# 中文否定词（否定窗口用）。刻意排除单字“未/无/非/难”等歧义字，
# 避免“未来”“无锡”“非洲”等词被误判为否定。
CN_NEGATORS = ["不会", "不能", "未能", "没有", "并未", "不再", "尚未", "难以",
               "无法", "避免", "缺乏", "不及", "不", "没", "负"]

# ---------- 英文金融词典 ----------
# 含空格或连字符的短语走子串匹配；单词走边界匹配 + 否定窗口。

EN_POS = [
    "rate cut", "rate cuts", "cut rates", "beat estimates", "beat expectations",
    "better-than-expected", "better than expected", "record high", "all-time high",
    "all time high", "raise guidance", "raises guidance", "top estimates",
    "top expectations", "ceasefire", "peace deal", "trade deal",
    "surge", "surges", "surged", "rally", "rallies", "rallied",
    "soar", "soars", "soared", "jump", "jumps", "jumped",
    "gain", "gains", "gained", "upgrade", "upgrades", "upgraded",
    "outperform", "outperforms", "bullish", "upbeat", "beat", "beats",
    "breakthrough", "buyback", "buybacks", "dividend", "stimulus",
    "easing", "recovery", "dovish", "truce", "approved", "approval",
    "expansion", "boost", "boosted", "growth", "record", "strong",
    "optimistic", "profit", "profits", "winner", "winners", "positive",
]

EN_NEG = [
    "rate hike", "rate hikes", "hike rates", "below estimates",
    "below expectations", "worse-than-expected", "worse than expected",
    "cut guidance", "cuts guidance", "trade war", "bear market",
    "sell-off", "selloff", "risk-off", "risk off", "state of emergency",
    "plunge", "plunges", "plunged", "crash", "crashed", "slump", "slumped",
    "drop", "drops", "dropped", "decline", "declines", "declined",
    "downgrade", "downgrades", "downgraded", "underperform", "bearish",
    "downbeat", "miss", "misses", "missed", "layoff", "layoffs",
    "default", "defaults", "recession", "contraction", "warning", "warns",
    "warned", "crisis", "investigation", "probe", "lawsuit", "fined",
    "sanction", "sanctions", "tariff", "tariffs", "ban", "banned",
    "halt", "halts", "halted", "suspend", "suspended", "recall",
    "shortfall", "loss", "losses", "bankruptcy", "fraud", "fraudulent",
    "hawkish", "hike", "hikes", "hiked", "tightening", "escalation",
    "escalate", "conflict", "war", "invasion", "negative",
]

EN_NEGATORS = {"not", "no", "never", "without", "hardly", "barely", "n't", "nt"}


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _split_en_terms(terms):
    phrases = [t for t in terms if re.search(r"[\s-]", t)]
    words = {t for t in terms if not re.search(r"[\s-]", t)}
    return phrases, words


EN_POS_PHRASES, EN_POS_WORDS = _split_en_terms(EN_POS)
EN_NEG_PHRASES, EN_NEG_WORDS = _split_en_terms(EN_NEG)


def _negated(words, i: int) -> bool:
    for j in range(max(0, i - 3), i):
        w = words[j].rstrip(".,!?;:")
        if w in EN_NEGATORS or w.endswith("n't"):
            return True
    return False


def _occurrences(text: str, word: str) -> list:
    """返回 word 在 text 中的所有起始下标（不重叠，等价于 str.count 的语义）。"""
    idxs = []
    start = 0
    while True:
        i = text.find(word, start)
        if i < 0:
            break
        idxs.append(i)
        start = i + len(word)
    return idxs


def _negated_before(text: str, pos: int) -> bool:
    """判断 pos 前 4 字符窗口内是否出现否定词（中文否定窗口）。"""
    window = text[max(0, pos - 4):pos]
    return any(ng in window for ng in CN_NEGATORS)


def score_text_zh(text: str) -> float:
    if not text:
        return 0.0
    t = str(text).lower()
    pos = neg = 0
    for w in CN_POS:
        for i in _occurrences(t, w):
            if _negated_before(t, i):
                neg += 1
            else:
                pos += 1
    for w in CN_NEG:
        for i in _occurrences(t, w):
            if _negated_before(t, i):
                pos += 1
            else:
                neg += 1
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg + 1)


def score_text_en(text: str) -> float:
    if not text:
        return 0.0
    t = str(text).lower()
    pos = neg = 0
    for ph in EN_POS_PHRASES:
        pos += t.count(ph)
    for ph in EN_NEG_PHRASES:
        neg += t.count(ph)
    words = re.findall(r"[a-z']+", t)
    for i, raw in enumerate(words):
        w = raw.rstrip(".,!?;:")
        if w in EN_POS_WORDS or w in EN_NEG_WORDS:
            flip = _negated(words, i)
            if w in EN_POS_WORDS:
                if flip:
                    neg += 1
                else:
                    pos += 1
            else:
                if flip:
                    pos += 1
                else:
                    neg += 1
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg + 1)


def _lexicon_score(text: str) -> float:
    """词典打分（语言自动路由），永远可用，作为各后端的兜底。"""
    if not text:
        return 0.0
    s = str(text)
    if _is_cjk(s):
        return score_text_zh(s)
    return score_text_en(s)


# ================= 后端抽象 =================

class SentimentBackend:
    """情绪打分后端基类。实现 score / score_batch。"""
    name = "base"

    def score(self, text: str) -> float:
        raise NotImplementedError

    def score_batch(self, texts: list) -> list:
        return [self.score(t) for t in texts]


class LexiconBackend(SentimentBackend):
    name = "lexicon"

    def score(self, text: str) -> float:
        return _lexicon_score(text)


class FinBertBackend(SentimentBackend):
    """FinBERT 系列后端。惰性加载，任何异常回退词典。"""
    name = "finbert"

    def __init__(self, model_name: str = "yiyanghkust/finbert-tone",
                 device=None, batch_size: int = 16):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._pipe = None
        self._ok = False
        self._err = None

    def _ensure(self) -> bool:
        if self._ok:
            return True
        if self._err is not None:
            return False
        try:
            from transformers import pipeline  # noqa: E402
            self._pipe = pipeline("text-classification", model=self.model_name,
                                  top_k=None, device=self.device)
            self._ok = True
        except Exception as e:  # noqa: BLE001
            self._err = e
        return self._ok

    def _map(self, out) -> float:
        pos = neg = 0.0
        for r in out or []:
            lab = str(r.get("label") or "").strip().lower()
            s = float(r.get("score") or 0.0)
            if "positive" in lab or "bullish" in lab or lab in ("pos", "up", "gain", "gains"):
                pos += s
            elif "negative" in lab or "bearish" in lab or lab in ("neg", "down", "loss", "losses"):
                neg += s
        return pos - neg

    def score(self, text: str) -> float:
        if not self._ensure():
            return _lexicon_score(text)
        try:
            return self._map(self._pipe(str(text))[0])
        except Exception:  # noqa: BLE001
            return _lexicon_score(text)

    def score_batch(self, texts: list) -> list:
        if not texts:
            return []
        if not self._ensure():
            return [_lexicon_score(t) for t in texts]
        try:
            outs = self._pipe([str(t) for t in texts], batch_size=self.batch_size)
            return [self._map(o) for o in outs]
        except Exception:  # noqa: BLE001
            return [_lexicon_score(t) for t in texts]


class LLMBackend(SentimentBackend):
    """OpenAI 兼容 chat/completions 后端，结构化 JSON 输出 sentiment。"""
    name = "llm"

    def __init__(self, api_base: str = "", api_key: str = "",
                 model: str = "", timeout: int = 20):
        self.api_base = (api_base or os.environ.get("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout

    def score(self, text: str) -> float:
        if not self.api_key:
            return _lexicon_score(text)
        try:
            import requests  # noqa: E402
            prompt = (
                "你是金融新闻情绪分析师。请对下面的新闻打分，只返回 JSON："
                '{"sentiment": 在[-1,1]之间的情绪分, "reason": "一句话理由"}\n\n'
                f"新闻：{text}"
            )
            r = requests.post(
                f"{self.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=self.timeout,
            )
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            obj = json.loads(m.group(0)) if m else {}
            val = float(obj.get("sentiment", 0.0))
            return max(-1.0, min(1.0, val))
        except Exception:  # noqa: BLE001
            return _lexicon_score(text)


# ================= 动态路由 =================

_active_backend: SentimentBackend = LexiconBackend()


def set_backend(backend: SentimentBackend) -> None:
    global _active_backend
    _active_backend = backend


def get_backend() -> SentimentBackend:
    return _active_backend


def score_text(text: str) -> float:
    """统一入口：路由到当前后端（默认词典）。"""
    if not text:
        return 0.0
    return _active_backend.score(str(text))


def score_batch(texts: list) -> list:
    return _active_backend.score_batch([str(t) for t in (texts or [])])


def configure_backend(cfg: dict) -> SentimentBackend:
    """按 config['sentiment'] 构建并激活后端。失败不抛异常，回退词典。"""
    global _active_backend
    s = (cfg.get("sentiment") or {}) if isinstance(cfg, dict) else {}
    name = str(s.get("backend") or "lexicon").strip().lower()

    if name == "finbert":
        _active_backend = FinBertBackend(
            model_name=s.get("finbert_model") or "yiyanghkust/finbert-tone",
            device=s.get("finbert_device") or None,
        )
        print(f"[sentiment] 后端=finbert（{_active_backend.model_name}，首次评分时加载，失败回退词典）")
    elif name == "llm":
        _active_backend = LLMBackend(
            api_base=s.get("llm_api_base") or "",
            api_key=s.get("llm_api_key") or "",
            model=s.get("llm_model") or "",
        )
        print(f"[sentiment] 后端=llm（{_active_backend.model}，失败回退词典）")
    else:
        _active_backend = LexiconBackend()
        print("[sentiment] 后端=lexicon（离线词典）")

    return _active_backend
