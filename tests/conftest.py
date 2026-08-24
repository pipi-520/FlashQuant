"""pytest 公共配置：把项目根目录加入 sys.path，便于 import 各包。"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
