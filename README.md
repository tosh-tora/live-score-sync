# 汎用オーケストラ追従スライド制御システム

ライブオーケストラの生演奏をマイクでリアルタイム解析し、MusicXML 形式の楽譜と照合することで、指定小節番号に到達した瞬間に **Google Slides** のスライドを自動制御するシステムです。

## 構成

このリポジトリは 2 つのタスクを含みます:

- **Task 1 (Compressor)**: フルスコアからガイド譜を生成する CLI ツール。Windows/macOS/Linux/WSL2 どこでも動作。
- **Task 2 (Sequential Live Follower)**: マイク追従 + Google Slides 制御のリアルタイムアプリ。**WSL2 (Ubuntu) 上での動作前提**。

> **なぜ WSL2 必須か**: 追従アルゴリズムに利用する `pymatchmaker` (Matchmaker) は Windows wheel を提供していないため。Windows 11 の WSLg がマイク入力・GUI 表示をホストとシームレスに繋ぐので、操作 GUI もスライドも WSL2 内で完結します。

詳細なセットアップ手順は [`INSTALLATION_JP.md`](INSTALLATION_JP.md) を参照してください。

## プロジェクト構成

```
live-score-sync/
├── compressor.py                      # Task 1: MusicXML 圧縮
├── compressor_config.example.json     # 圧縮設定テンプレート
├── requirements.txt                   # Python 依存パッケージ
├── work/
│   ├── inbox/                         # Task 1 入力 (フルスコア)
│   └── outbox/                        # Task 1 出力 (圧縮ガイド譜)
├── sequential_live_follower/          # Task 2: メインアプリケーション
│   ├── main.py                        # エントリーポイント (CLI)
│   ├── config.json                    # 設定ファイル (gitignore 対象・ユーザー作成)
│   ├── config_example.json            # 設定テンプレート
│   ├── core/
│   │   ├── matcher.py                 # pymatchmaker (Matchmaker) ラッパ
│   │   ├── slide_controller.py        # Playwright (Google Slides 制御)
│   │   ├── score_mapper.py            # 拍 ↔ 小節変換 (partitura)
│   │   ├── state_manager.py           # スレッドセーフ状態管理
│   │   ├── inertia_engine.py          # 信頼度低下時の慣性補外
│   │   └── cooldown_timer.py          # トリガー連発防止
│   ├── ui/
│   │   └── gui_tkinter.py             # 操作 GUI
│   └── config/
│       └── loader.py                  # config.json 解析・バリデーション
├── INSTALLATION_JP.md                 # セットアップ手順 (WSL2)
├── CLAUDE.md                          # 開発ガイダンス
└── specification.txt                  # システム仕様書
```

## Task 1: MusicXML 圧縮 (Compressor)

**目的**: フルオーケストラスコアから「音楽的に重要なパート」を自動抽出し、リアルタイム追従に適した軽量ガイド譜を生成。

```bash
# work/inbox/ にある全 MusicXML をバッチ処理
python compressor.py

# 単一ファイル + カスタム重み
python compressor.py score.xml -c compressor_config.json --weight-rhythm 5.0
```

**主要機能:**

- 「活動量スコア」に基づくパート自動選定
- 設定可能な重み (音符数 / リズム解像度 / 音高分散)
- ユニゾン (同一タイミングの重複音) 統合
- 変拍子対応

出力は `work/outbox/` に保存されます。

## Task 2: Sequential Live Follower

**目的**: マイク入力をリアルタイムに追従し、指定小節到達時に Google Slides を自動操作。

### 起動手順

```bash
# 1. WSL2 (Ubuntu) ターミナルを開く

# 2. プロジェクトディレクトリへ移動
cd ~/live-score-sync/sequential_live_follower

# 3. 仮想環境を有効化 ← 毎回必要
source ../.venv/bin/activate
# プロンプトが (.venv) toshi@... に変わることを確認

# 4. アプリ起動
python -m sequential_live_follower.main sequential_live_follower/config.json \
    --slide-url "https://docs.google.com/presentation/d/<ID>/present" \
    -v
```

起動時に Chromium (Google Slides) と Tkinter 操作 GUI の 2 ウィンドウが立ち上がります。
Chromium をプロジェクタ側モニタにドラッグして F11 でフルスクリーン化し、操作 GUI は手元モニタに残します。

### ワークフロー

1. アプリ起動 → 最初の楽章 (`movements[0]`) が自動ロードされ、マイク追従が始まる
2. 演奏が進むと操作 GUI の現在小節・拍位置・確信度がリアルタイム更新される
3. `config.json` の `triggers[].measure` に到達するたびに、Playwright が Chromium にキーを送りスライドが進む
4. 操作 GUI にフォーカスしてキー操作でスライド制御・楽章切り替えが可能

### キーバインド

操作 GUI ウィンドウにフォーカスした状態で有効です。

| キー | 動作 |
|------|------|
| `→` / `Space` | スライドを 1 枚進める |
| `←` | スライドを 1 枚戻す |
| `N` | 次の楽章へ (スコアと追従状態をリセット) |
| `R` | 現在の楽章を先頭から再ロード (認識が外れたときのリカバリ) |

### 操作 GUI の表示項目

| 表示 | 内容 |
|------|------|
| 第 N 楽章 / 全 M 楽章 | 現在の楽章番号と総楽章数 |
| ファイル名 | 読み込み中のスコアファイル名（エラー時は赤字でメッセージ） |
| 小節番号（大） | 現在の小節（青色・大きなフォント） |
| N / M 小節目 | 小節番号 / 総小節数 |
| ♩ X.XX | 小節内の拍位置（1拍目 = 1.00） |
| 確信度バー | 追従の確かさ（緑 > 60%・橙 > 40%・赤 ≤ 40%） |
| 次のトリガー | 次にスライドが動く小節番号 |
| ⚠ 慣性モード（推定） | 確信度が低く、直前テンポで仮想進行中 |
| 🔒 クールダウン中 | トリガー連発防止のロック中 |
| マイク: XX dBFS | リアルタイムマイクレベルと無音判定状態 |

### 主要機能

- マイク入力 (`pymatchmaker` 内部の audio frontend)
- DTW ベース拍追跡 (`pymatchmaker.Matchmaker.run()`)
- サブウィンドウ CV による追従信頼度推定（無関係な音・無音での誤発火防止）
- 信頼度低下時の慣性モード (直前のテンポで仮想進行)
- 小節精度トリガー実行 + クールダウンによる誤発火防止
- 起動時の config.json バリデーション（構文エラー・必須項目欠如を即座に報告して終了）
- スコアファイル未検出時の配置先ガイダンス表示

## 設定ファイル

### config.json (Task 2 で必須)

`config.json` は `gitignore` 対象のユーザーローカルファイルです。`config_example.json` をコピーして編集してください。

**最小構成 — `xml_file` を省略すると自動検出**

`xml_file` を書かない場合、`config.json` と同じフォルダにある最初の `.mxl` ファイルが自動的に使われます。スコアが 1 ファイルだけなら、ファイル名を書く必要はありません。

```json
{
  "settings": {
    "cooldown_seconds": 3.0,
    "confidence_threshold": 0.4
  },
  "movements": [
    {
      "id": 1,
      "triggers": [
        { "measure": 1,  "action": "right", "note": "冒頭" },
        { "measure": 45, "action": "right", "note": "テーマA" }
      ]
    }
  ]
}
```

**`xml_file` を明示する場合**（複数楽章や別フォルダのファイルを使うとき）

```json
{
  "movements": [
    {
      "id": 1,
      "xml_file": "mv1.mxl",
      "triggers": [...]
    },
    {
      "id": 2,
      "xml_file": "mv2.mxl",
      "triggers": [...]
    }
  ]
}
```

**settings フィールド一覧**

| フィールド | デフォルト | 説明 |
|-----------|-----------|------|
| `cooldown_seconds` | `3.0` | トリガー発火後の再発火抑止時間（秒） |
| `confidence_threshold` | `0.4` | これを下回ると慣性モードへ移行 |
| `silence_threshold_db` | `-55.0` | この dBFS 以下のマイク入力を無音と判定し確信度を 0 にする |
| `inertia_timeout_seconds` | `5.0` | 慣性モードのタイムアウト（秒）。超過すると追従待機に戻る |
| `mic_device` | `"pulse"` | 音声入力デバイス。WSL2 では `"pulse"` が標準。整数インデックスまたはデバイス名も指定可 |

**triggers フィールド**

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `measure` | ✓ | トリガーを発火する小節番号（1 以上の整数） |
| `action` | ✓ | `"right"`（スライド進む）または `"left"`（スライド戻る） |
| `note` | — | 表示用メモ（任意） |

**スコアファイル**

- `xml_file` は `config.json` からの相対パスまたは絶対パスで指定
- 対応フォーマット: `.mxl`（圧縮 MusicXML）、`.xml`、`.musicxml`

### compressor_config.json (Task 1 オプション)

```json
{
  "weights": {
    "note_count": 1.0,
    "rhythmic_resolution": 3.0,
    "pitch_variance": 0.5
  },
  "top_n": 4
}
```

## システム要件

- **OS (Task 1)**: Windows / macOS / Linux / WSL2
- **OS (Task 2)**: Windows 11 + WSL2 (Ubuntu 24.04) を推奨。WSLg のマイク・GUI パススルーが必須。
- **Python**: 3.10 以上 (`pymatchmaker` 0.2.1 は 3.11/3.12/3.13 対応)
- **メモリ**: 2GB 以上推奨
- **オーディオ**: マイク入力デバイス
- **ディスプレイ**: 操作 GUI + プロジェクタの 2 系統

詳細は [`INSTALLATION_JP.md`](INSTALLATION_JP.md) を参照。

## アーキテクチャ概要

### スレッドモデル

```
メインスレッド
  └─ Tkinter 操作 GUI (各種状態を 100ms 周期で再描画)

ワーカースレッド (daemon)
  ├─ matchmaker-worker      Matchmaker.run() ジェネレータをドライブし、最新拍位置を AppState に書き込む
  ├─ slide-controller       Playwright で Chromium を保持し、Queue 経由でキー送信
  ├─ state-sync (20Hz)      matcher.get_latest() → InertiaEngine → ScoreMapper → AppState
  └─ trigger-executor (20Hz) AppState を監視し、目標小節到達で slide_controller.press()
```

### データフロー

```
マイク
  ↓ (pymatchmaker 内部で chroma 抽出 + DTW)
Matchmaker.run() ── beat ──→ MatchMaker.get_latest()
                                     ↓ (state-sync)
                              InertiaEngine
                                     ↓
                              ScoreMapper (beat → measure)
                                     ↓
                              AppState ─→ Tkinter GUI 表示
                                     ↓ (trigger-executor)
                              SlideController.press("right")
                                     ↓
                              Chromium (Google Slides) → 次スライド
```

### エラーハンドリング

- 起動時に `config.json` をバリデーション。構文エラー・必須項目欠如・不正な値は `ConfigError` として報告し、スレッド起動前に `exit(1)` する
- `xml_file` が存在しない場合は GUI に赤字でファイルの配置先パスを表示
- `xml_file` 未指定の場合は `config.json` と同じフォルダの `.mxl` を自動検出
- 各ワーカーのトップレベルで try/except、ログ出力
- マッチング信頼度低下時は `InertiaEngine` が前回テンポで仮想進行
- 連続発火防止: 同一小節への再トリガーは `cooldown_seconds` 経過まで抑止
- 楽章切り替え・`R` キーで `MatchMaker` インスタンスを破棄して再生成

## トラブルシューティング

| 症状 | 原因と対処 |
|------|-----------|
| 起動直後に `ConfigError` が表示される | `config.json` の JSON 構文エラー（カンマ忘れ・括弧ミス）またはフィールド不足。エラーメッセージに行番号と場所が表示される |
| GUI に「⚠ ファイルが見つかりません」と表示される | `xml_file` で指定したパスにファイルがない。表示されるパスにスコアファイルを置く |
| `python: command not found` | 仮想環境が有効でない。`source .venv/bin/activate` を実行してから再試行 |
| `from matchmaker import Matchmaker` が ImportError | WSL2 内の venv で `pip install pymatchmaker` を実行 |
| `Could not find FluidSynth library` | `sudo apt install fluidsynth libfluidsynth-dev` |
| sounddevice にデバイスが表示されない | `sudo apt install libasound2-plugins` + `~/.asoundrc` の設定 (INSTALLATION_JP.md 参照) |
| マイクが認識されない | `wsl --shutdown` 後に再起動し、Windows 側の入力デバイス設定を確認 |
| Chromium がクラッシュ | `playwright install chromium` 未実行 |
| トリガーでスライドが進まない | Chromium ウィンドウをクリックしてフォーカス、F11 でプレゼンモード化 |
| GUI に「マイク: 監視無効」と表示される | sounddevice がマイクを開けなかった。上記「sounddevice にデバイスが表示されない」を参照 |
| 確信度が常に低い / 慣性モードが続く | マイクゲイン調整、`confidence_threshold` を下げる、スコアが実演奏曲と一致しているか確認 |
| 演奏途中で認識が止まった | `R` キーで現在楽章を再ロード。改善しない場合は `N` → 前の楽章に戻す操作はできないので再起動 |
| `N` キーや `R` キーが効かない | 操作 GUI ウィンドウにフォーカスを当てる |

## リファレンス

- **pymatchmaker (Matchmaker)**: リアルタイム DTW 追従ライブラリ — https://github.com/pymatchmaker/matchmaker
- **Partitura**: MusicXML 構造解析 — https://partitura.readthedocs.io/
- **Playwright**: ブラウザ自動化 — https://playwright.dev/python/
- **Music21**: 音楽記譜法ライブラリ (Task 1 で利用)
- **Librosa**: オーディオ特徴抽出 (Task 1 補助)
