"""
SplitPromptRandomizerAndLoader - ComfyUI Custom Node (V3)

キャラクター名選出フィールドと一般プロンプト選出フィールドを分離し、
{A|B|C} 構文をシード制御でランダム解決した上で、
キャラクター名に対応するリファレンス画像を自動ロードするノード。
"""

from __future__ import annotations

import os
import re
import random
from typing import List

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


def _clean_text(text: str) -> str:
    """
    # または // で始まる行をコメントとして除外し、
    空行を整理して返す。
    """
    if not text or not text.strip():
        return ""

    lines = text.splitlines()
    kept: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        kept.append(line.rstrip())

    return "\n".join(kept).strip()


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
                io.String.Output("character_output", tooltip="解決済みキャラクター名"),
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
        costume_id: int,
        seed: int,
    ):
        rng = random.Random(seed)

        # 1. コメント行除外
        char_clean = _clean_text(character_text)
        gen_clean = _clean_text(general_text)

        # 2. {A|B|C} ランダム解決（ネスト対応）
        # シードの消費順を安定させるため、character → general の順で解決
        character_output = _resolve_random(char_clean, rng).strip()
        general_output = _resolve_random(gen_clean, rng).strip()

        # 3. 画像マッチング（costume_id で COS 指定、なければ基本画像）
        package_root = os.path.dirname(os.path.abspath(__file__))
        full_dir = os.path.join(package_root, character_dir.strip().lstrip("/\\"))

        matched_path = _find_matching_image(
            character_output, full_dir, costume_id=costume_id
        )

        if matched_path and os.path.isfile(matched_path):
            try:
                character_image = _load_image_as_tensor(matched_path)
            except Exception:
                character_image = _black_image()
        else:
            character_image = _black_image()

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
