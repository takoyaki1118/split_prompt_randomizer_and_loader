# Split Prompt Randomizer & Loader

ComfyUI カスタムノード。  
キャラクター名選出フィールドと一般プロンプト選出フィールドを分離し、`{A|B|C}` 構文をシード制御でランダム解決した上で、選ばれたキャラクター名に対応するリファレンス画像を自動ロードします。

## 機能

- **2つの独立したテキストフィールド**
  - `character_text` : キャラクター名専用
  - `general_text` : 一般プロンプト専用
- **コメント行の除外**
  - `#` または `//` で始まる行は完全に無視されます
- **`{A|B|C}` ランダム構文**
  - パイプ区切りの選択肢からランダムに1つを選択
  - ネスト（入れ子）および複数ブロックに対応
  - `seed` で再現可能
- **ゆらぎ吸収マッチング**
  - キャラクター名と画像ファイル名を比較する際に以下を正規化
    - すべて小文字化
    - 半角スペース / アンダーバー `_` / ハイフン `-` を除去
  - 例: `yakumo yukari` ↔ `yakumo_yukari.png` / `YakumoYukari.jpg`
- **コスチューム指定（costume_id）**
  - `costume_id = 0` → 基本画像（`yakumo_yukari.png`）
  - `costume_id = 1` → `yakumo_yukari_COS1.png` などを探す
  - 指定番号の COS が存在しない場合は **基本画像にフォールバック**
  - プロンプト文字列には COS 接尾辞は一切入らない
- **リファレンス画像出力**
  - マッチした画像を `IMAGE` として出力
  - 見つからない場合や空欄の場合は **512×512 の黒画像** を出力

## インストール

1. このフォルダを `ComfyUI/custom_nodes/` にコピー
2. ComfyUI を再起動

または ZIP を展開して配置。

## キャラクター画像の置き場所

**このパッケージ内の `characters/` フォルダ** に画像を置いてください。

```
split_prompt_randomizer_and_loader/
├── __init__.py
├── nodes.py
├── characters/               ← ここにキャラ画像を置く
│   ├── yakumo_yukari.png
│   ├── hakurei_reimu.jpg
│   └── ...
├── pyproject.toml
└── README.md
```

- `character_dir` のデフォルトは `"characters"`（パッケージ内相対パス）
- 別フォルダを使いたい場合は `character_dir` を変更（例: `"refs"` → パッケージ内の `refs/` を見る）

## 使い方

1. ノード検索で **「Split Prompt Randomizer & Loader」** を追加
2. パッケージ内の `characters/` にキャラクター画像を配置
3. テキストフィールドにプロンプトを記入

### 入力例

**character_text**
```
# 東方キャラ
{yakumo yukari|hakurei reimu|kirisame marisa}
```

**general_text**
```
{masterpiece, best quality|high quality}, {1girl|2girls}, {outdoors|indoors}
```

同じ `seed` 値なら常に同じ選択結果になります。

## 入出力仕様

### 入力

| 名前 | 型 | デフォルト | 説明 |
|------|-----|------------|------|
| character_text | STRING (multiline) | "" | キャラクター名選出専用 |
| general_text | STRING (multiline) | "" | 一般プロンプト選出専用 |
| character_dir | STRING | "characters" | このパッケージ内の相対パス |
| costume_id | INT | 0 | コスチューム番号（0=基本、1以上=COS番号。なければ基本にフォールバック） |
| seed | INT | 0 | 乱数シード |

### 出力

| 名前 | 型 | 説明 |
|------|-----|------|
| character_output | STRING | 解決済みキャラクター名 |
| general_output | STRING | 解決済み一般プロンプト |
| character_image | IMAGE | マッチしたリファレンス画像（なければ黒画像） |

## ライセンス

MIT
