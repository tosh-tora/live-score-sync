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
│   ├── config_example.json            # 設定テンプレート
│   ├── README.md / README_JP.md       # 詳細仕様
│   ├── core/
│   │   ├── matcher.py                 # pymatchmaker (Matchmaker) ラッパ
│   │   ├── slide_controller.py        # Playwright (Google Slides 制御)
│   │   ├── score_mapper.py            # 拍 ↔ 小節変換 (partitura)
│   │   ├── state_manager.py           # スレッドセーフ状態管理
│   │   ├── inertia_engine.py          # 信頼度低下時の慣性補外
│   │   └── cooldown_timer.py          # トリガー連発防止
│   ├── ui/
│   │   └── gui_tkinter.py             # 操作 GUI (現在小節・信頼度表示)
│   └── config/
│       └── loader.py                  # config.json 解析
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

### 起動

```bash
# WSL2 内、仮想環境を有効化した状態で
python -m sequential_live_follower.main config.json \
    --slide-url "https://docs.google.com/presentation/d/<ID>/present" \
    -v
```

起動時に Chromium (Google Slides) と Tkinter 操作 GUI の 2 ウィンドウが立ち上がります。
Chromium をプロジェクタ側モニタにドラッグして F11 でフルスクリーン化し、操作 GUI は手元モニタに残します。

### ワークフロー

1. アプリ起動 → 最初の楽章 (`movements[0]`) が自動ロードされ、マイク追従が始まる
2. 演奏が進むと操作 GUI の「現在小節」がリアルタイム更新
3. config.json の `triggers[].measure` に到達するたびに、Playwright が Chromium にキーを送り、スライドが進む
4. 操作 GUI にフォーカスして `N` キーを押すと、次の楽章 (`movements[1]`) がロードされる

### 主要機能

- マイク入力 (`pymatchmaker` 内部の audio frontend)
- DTW ベース拍追跡 (`pymatchmaker.Matchmaker.run()`)
- 信頼度低下時の慣性モード (直前のテンポで仮想進行)
- 小節精度トリガー実行 + クールダウンによる誤発火防止
- 操作 GUI (現在小節 / 信頼度 / 次トリガー / 慣性モード表示)
- Google Slides 自動制御 (Playwright)

## 設定ファイル

### config.json (Task 2 で必須)

```json
{
  "settings": {
    "cooldown_seconds": 3.0,
    "confidence_threshold": 0.4
  },
  "movements": [
    {
      "id": 1,
      "xml_file": "work/outbox/mv1_guide.xml",
      "triggers": [
        { "measure": 1,  "action": "right", "note": "楽章1 開始" },
        { "measure": 45, "action": "right", "note": "テーマA" }
      ]
    }
  ]
}
```

`action` は `"right"`, `"left"`, `"up"`, `"down"`, `"space"`, `"enter"` などに対応 (内部で Playwright の `ArrowRight` 等にマップされます)。

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
  └─ Tkinter 操作 GUI (現在小節・信頼度を 100ms 周期で再描画)

ワーカースレッド (daemon)
  ├─ matchmaker-worker      Matchmaker.run() ジェネレータをドライブし、最新拍位置を AppState に書き込む準備
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

- 各ワーカーのトップレベルで try/except、ログ出力
- マッチング信頼度低下時は `InertiaEngine` が前回テンポで仮想進行
- 連続発火防止: 同一小節への再トリガーは `cooldown_seconds` 経過まで抑止
- 楽章切り替え時に `MatchMaker` インスタンスを破棄して再生成 (Matchmaker は内部音声ストリームを所有しているため)

## トラブルシューティング

| 症状 | 原因と対処 |
|------|-----------|
| `from matchmaker import Matchmaker` が ImportError | WSL2 内の venv で `pip install pymatchmaker` を実行 |
| `Could not find FluidSynth library` | `sudo apt install fluidsynth libfluidsynth-dev` |
| マイクが認識されない | `wsl --shutdown` 後に再起動し、Windows 側の入力デバイス設定を確認 |
| Chromium がクラッシュ | `playwright install chromium` 未実行 |
| トリガーでスライドが進まない | Chromium ウィンドウをクリックしてフォーカス、F11 でプレゼンモード化 |
| 信頼度が常に低い | マイクゲイン調整、`confidence_threshold` を下げる、ガイド譜が実演奏曲と一致しているか確認 |
| `N` キーで楽章送りできない | 操作 GUI ウィンドウにフォーカスを当てる |

## リファレンス

- **pymatchmaker (Matchmaker)**: リアルタイム DTW 追従ライブラリ — https://github.com/pymatchmaker/matchmaker
- **Partitura**: MusicXML 構造解析 — https://partitura.readthedocs.io/
- **Playwright**: ブラウザ自動化 — https://playwright.dev/python/
- **Music21**: 音楽記譜法ライブラリ (Task 1 で利用)
- **Librosa**: オーディオ特徴抽出 (Task 1 補助)
