"""
SplitPromptRandomizerAndLoader - ComfyUI Custom Node (V3)

キャラクター名選出フィールドと一般プロンプト選出フィールドを分離し、
{A|B|C} 構文をシード制御でランダム解決した上で、
キャラクター名に対応するリファレンス画像を自動ロードするノード。
"""

from __future__ import annotations

import json
import os
import re
import random
from typing import Any, Dict, List, Optional

import numpy as np
import torch
from PIL import Image, ImageOps
from typing_extensions import override

from comfy_api.latest import ComfyExtension, io


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """マッチング用正規化: 小文字化 + スペース/アンダースコア/ハイフン除去"""
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"[\s_\-]+", "", name)
    return name


def _split_top_level(text: str, delimiter: str = "|") -> List[str]:
    """ブレース深度0の位置でのみ delimiter で分割する"""
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    for c in text:
        if c == "{":
            depth += 1
            current.append(c)
        elif c == "}":
            depth -= 1
            current.append(c)
        elif c == delimiter and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(c)
    if current or not parts:
        parts.append("".join(current).strip())
    return [p for p in parts if p is not None]


def _resolve_random(text: str, rng: random.Random) -> str:
    """
    {選択肢1|選択肢2|...} を再帰的に解決する。
    ネストおよび複数ブロックに対応。
    """
    if not text:
        return ""

    def parse(s: str) -> str:
        result: List[str] = []
        i = 0
        n = len(s)
        while i < n:
            if s[i] == "{":
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if s[j] == "{":
                        depth += 1
                    elif s[j] == "}":
                        depth -= 1
                    j += 1
                if depth != 0:
                    # 閉じ忘れは原文のまま扱う
                    result.append(s[i])
                    i += 1
                    continue
                inner = s[i + 1 : j - 1]
                options = _split_top_level(inner, "|")
                chosen = rng.choice(options) if options else ""
                result.append(parse(chosen))
                i = j
            else:
                result.append(s[i])
                i += 1
        return "".join(result)

    return parse(text)


def _extract_content_lines(text: str) -> List[str]:
    """
    # または // で始まる行と空行を除外し、
    中身のある行だけをリストで返す。
    各行が1つの「グループ」（選択肢ブロック）になる。
    """
    if not text or not text.strip():
        return []

    kept: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        kept.append(stripped)
    return kept


def _pick_group_line(
    lines: List[str],
    group_id: int,
    rng: random.Random,
) -> str:
    """
    group_id で行を選択する。
      0 → 全行からランダムに1行
      1..N → N番目の行（1始まり）。範囲外ならランダム
    行が空なら "" を返す。
    """
    if not lines:
        return ""
    if group_id <= 0:
        return rng.choice(lines)
    idx = group_id - 1
    if 0 <= idx < len(lines):
        return lines[idx]
    # 範囲外はランダムにフォールバック
    return rng.choice(lines)


def _clean_text(text: str) -> str:
    """後方互換用: コメント除外して結合した文字列を返す"""
    lines = _extract_content_lines(text)
    return "\n".join(lines).strip()


def _load_prompt_json(path: str) -> Dict[str, Any]:
    """JSON を読み込む。失敗・不存在時は空 dict。"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _lookup_prompt_from_json(
    data: Dict[str, Any],
    character_name: str,
    costume_id: int = 0,
) -> Optional[str]:
    """
    JSON からキャラクター名に対応するプロンプトを取得する。

    対応フォーマット:
      1) フラット:
         { "higuchi kaede": "higuchi kaede, antlers, ..." }

      2) コスチューム付き:
         {
           "my_oc": {
             "0": "1girl, silver hair, ...",
             "1": "1girl, silver hair, school uniform, ...",
             "base": "...",   # "0" の別名
             "cos1": "..."    # "1" の別名
           }
         }

    名前は正規化してマッチ。見つからなければ None（呼び出し側で従来どおり名前を使う）。
    """
    if not data or not character_name:
        return None

    target = _normalize_name(character_name)
    if not target:
        return None

    # キーを正規化して検索
    entry: Any = None
    for key, value in data.items():
        if _normalize_name(str(key)) == target:
            entry = value
            break

    if entry is None:
        return None

    # 文字列ならそのまま（costume 無視）
    if isinstance(entry, str):
        text = entry.strip()
        return text if text else None

    # dict なら costume_id で引く
    if isinstance(entry, dict):
        # 候補キーの優先順
        candidates: List[str] = []
        if costume_id >= 1:
            candidates.extend([
                str(costume_id),
                f"cos{costume_id}",
                f"COS{costume_id}",
                f"costume{costume_id}",
            ])
        candidates.extend(["0", "base", "default", ""])

        for ck in candidates:
            if ck in entry and isinstance(entry[ck], str) and entry[ck].strip():
                return entry[ck].strip()
            # 大文字小文字ゆらぎ
            for ek, ev in entry.items():
                if str(ek).lower() == ck.lower() and isinstance(ev, str) and ev.strip():
                    return ev.strip()

        # どれも無ければ最初の文字列値
        for ev in entry.values():
            if isinstance(ev, str) and ev.strip():
                return ev.strip()

    return None


def _load_image_as_tensor(path: str) -> torch.Tensor:
    """PIL で読み込み、ComfyUI 標準の (1, H, W, 3) float32 tensor に変換"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]  # (1, H, W, 3)


def _black_image(width: int = 512, height: int = 512) -> torch.Tensor:
    """512x512 黒画像 (1, H, W, 3)"""
    return torch.zeros((1, height, width, 3), dtype=torch.float32)


def _find_matching_image(
    character_name: str,
    directory: str,
    costume_id: int = 0,
) -> str | None:
    """
    character_name とファイル名（拡張子除く）を正規化比較してマッチするパスを返す。

    コスチューム指定:
      - costume_id == 0 → 基本画像（完全一致）を返す
      - costume_id >= 1 → キャラ名 + COS{番号} を探す
        例: yakumo_yukari_COS1.png / yakumo_yukari_cos2.png
      - 指定番号の COS が見つからない場合は基本画像にフォールバック
      - 基本画像もなければ None
    プロンプト側の文字列には影響しない。
    """
    if not character_name or not os.path.isdir(directory):
        return None

    target = _normalize_name(character_name)
    if not target:
        return None

    supported_ext = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"}

    base_path: str | None = None
    cos_path: str | None = None
    cos_suffix = f"cos{costume_id}"  # 正規化後は "cos1", "cos2"...

    for fname in os.listdir(directory):
        base, ext = os.path.splitext(fname)
        if ext.lower() not in supported_ext:
            continue
        norm = _normalize_name(base)

        # 基本画像（完全一致）
        if norm == target:
            base_path = os.path.join(directory, fname)
            continue

        # COS指定ありの場合: target + "cos" + 番号 で終わる／一致するもの
        if costume_id >= 1:
            # yakumoyukaricos1 / yakumoyukari_cos1 → 正規化後 yakumoyukaricos1
            if norm == target + cos_suffix or norm.startswith(target) and norm.endswith(cos_suffix):
                cos_path = os.path.join(directory, fname)

    # 優先順位: 指定COS → 基本画像
    if costume_id >= 1 and cos_path is not None:
        return cos_path
    return base_path


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class SplitPromptRandomizerAndLoader(io.ComfyNode):
    """
    キャラクター名と一般プロンプトを分離して {A|B|C} をランダム解決し、
    キャラクター名にマッチするリファレンス画像を自動ロードする。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SplitPromptRandomizerAndLoader",
            display_name="Split Prompt Randomizer & Loader",
            category="prompt/randomizer",
            description=(
                "キャラクター名選出と一般プロンプト選出を分離し、"
                "{A|B|C} 構文をシード制御でランダム解決した上で、"
                "キャラクター名に対応するリファレンス画像を自動ロードする。"
            ),
            inputs=[
                io.String.Input(
                    "character_text",
                    multiline=True,
                    default="",
                    tooltip="キャラクター名選出専用。{A|B|C} 構文とコメント(# //)対応",
                ),
                io.String.Input(
                    "general_text",
                    multiline=True,
                    default="",
                    tooltip="一般プロンプト選出専用。{A|B|C} 構文とコメント(# //)対応",
                ),
                io.String.Input(
                    "character_dir",
                    default="characters",
                    tooltip="このノードパッケージ内の相対パス。リファレンス画像フォルダ（デフォルト: characters/）",
                ),
                io.String.Input(
                    "prompt_json",
                    default="prompts.json",
                    tooltip=(
                        "プロンプト定義JSONのパス（パッケージルートまたは character_dir からの相対）。"
                        "名前がJSONにあればそのプロンプトを使用、なければ名前そのものを出力"
                    ),
                ),
                io.Int.Input(
                    "group_id",
                    default=0,
                    min=0,
                    max=99,
                    tooltip=(
                        "キャラクターのグループ行指定。"
                        "0=全グループからランダム、"
                        "1=1行目、2=2行目...（//コメント行は数えない）"
                    ),
                ),
                io.Int.Input(
                    "costume_id",
                    default=0,
                    min=0,
                    max=99,
                    tooltip="コスチューム番号。0=基本画像、1以上=COS1/COS2... 該当がなければ基本画像にフォールバック",
                ),
                io.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    tooltip="乱数シード。同じ値なら同じ選択結果を再現",
                ),
            ],
            outputs=[
                io.String.Output("character_output", tooltip="解決済みキャラクター名（またはJSONプロンプト）"),
                io.String.Output("general_output", tooltip="解決済み一般プロンプト"),
                io.Image.Output("character_image", tooltip="マッチしたリファレンス画像（なければ黒画像）"),
            ],
        )

    @classmethod
    def execute(
        cls,
        character_text: str,
        general_text: str,
        character_dir: str,
        prompt_json: str,
        group_id: int,
        costume_id: int,
        seed: int,
    ):
        rng = random.Random(seed)

        # 1. コメント除外 → 中身のある行だけ抽出
        char_lines = _extract_content_lines(character_text)
        gen_lines = _extract_content_lines(general_text)

        # 2. グループ行を1つ選ぶ → その行の {A|B|C} を解決
        char_line = _pick_group_line(char_lines, group_id, rng)
        gen_line = _pick_group_line(gen_lines, 0, rng)

        character_name = _resolve_random(char_line, rng).strip()
        general_output = _resolve_random(gen_line, rng).strip()

        package_root = os.path.dirname(os.path.abspath(__file__))
        full_dir = os.path.join(package_root, character_dir.strip().lstrip("/\\"))

        # 3. 画像マッチングは「名前」ベース（JSON展開前）
        matched_path = _find_matching_image(
            character_name, full_dir, costume_id=costume_id
        )

        if matched_path and os.path.isfile(matched_path):
            try:
                character_image = _load_image_as_tensor(matched_path)
            except Exception:
                character_image = _black_image()
        else:
            character_image = _black_image()

        # 4. JSON からプロンプト取得（なければ名前をそのまま出力）
        json_path = (prompt_json or "prompts.json").strip().lstrip("/\\")
        # 探索順: character_dir 内 → パッケージルート
        candidates = [
            os.path.join(full_dir, json_path),
            os.path.join(package_root, json_path),
            os.path.join(package_root, "characters", json_path),
        ]
        prompt_data: Dict[str, Any] = {}
        for p in candidates:
            prompt_data = _load_prompt_json(p)
            if prompt_data:
                break

        json_prompt = _lookup_prompt_from_json(
            prompt_data, character_name, costume_id=costume_id
        )
        character_output = json_prompt if json_prompt else character_name

        return io.NodeOutput(character_output, general_output, character_image)


# ---------------------------------------------------------------------------
# Extension entry
# ---------------------------------------------------------------------------

class SplitPromptRandomizerExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [SplitPromptRandomizerAndLoader]


async def comfy_entrypoint() -> SplitPromptRandomizerExtension:
    return SplitPromptRandomizerExtension()
