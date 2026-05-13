# 汎用オーケストラ追従スライド制御システム

ライブオーケストラの生演奏をマイクでリアルタイム解析し、MusicXML形式の楽譜と照合することで、指定小節番号に到達した瞬間にPowerPointのスライドを自動制御するシステムです。

## プロジェクト構成

```
mmatch/
├── compressor.py                      # Task 1: MusicXML圧縮
├── compressor_config.example.json     # 圧縮設定テンプレート
├── requirements.txt                   # Python依存パッケージ
├── work/
│   ├── inbox/                         # 入力ディレクトリ（フルスコア用）
│   └── outbox/                        # 出力ディレクトリ（圧縮楽譜）
├── sequential_live_follower/          # Task 2: メインアプリケーション
│   ├── main.py                        # エントリーポイント
│   ├── config_example.json            # 設定テンプレート
│   ├── README.md                      # 詳細ドキュメント（英語）
│   ├── README_JP.md                   # 詳細ドキュメント（日本語）
│   ├── core/
│   │   ├── audio_capturer.py          # PyAudio ストリームキャプチャ
│   │   ├── feature_extractor.py       # Chroma 特徴抽出
│   │   ├── matcher.py                 # PyMatcher DTW 統合
│   │   ├── score_mapper.py            # Beat ↔ 小節変換
│   │   ├── state_manager.py           # スレッドセーフ状態管理
│   │   ├── inertia_engine.py          # 信頼度ベース自動補外
│   │   ├── cooldown_timer.py          # トリガー連発防止
│   │   └── keyboard_listener.py       # （予定）グローバルホットキー
│   ├── ui/
│   │   ├── gui_tkinter.py             # リアルタイム表示
│   │   └── layouts.py                 # （予定）UI テーマ
│   ├── config/
│   │   └── loader.py                  # JSON設定ファイル解析
│   └── utils/
│       └── logger.py                  # （予定）ロギング設定
├── CLAUDE.md                          # 開発ガイダンス
└── specification.txt                  # システム仕様書
```

## 2部構成システム

### Task 1: MusicXML圧縮（前処理）

**目的**: フルオーケストラスコアから「音楽的に重要なパート」を自動抽出し、リアルタイムマッチングに適した軽量ガイド楽譜を生成

**使用方法:**
```bash
# 単一ファイル処理
python compressor.py score.xml

# バッチ処理（work/inbox内の全ファイル）
python compressor.py

# カスタム重み指定
python compressor.py score.xml -c compressor_config.json --weight-rhythm 5.0
```

**出力**: 圧縮されたガイド楽譜を `work/outbox/` に保存

**主要機能:**
- 自動パート選定（「活動量スコア」基づく）
- 設定可能な重み付け（音符数、リズム解像度、音高分散）
- ユニゾン除去（同一タイミングの重複音を統合）
- 変拍子対応

### Task 2: Sequential Live Follower（メインアプリ）

**目的**: リアルタイム演奏追跡とスライド自動化

**使用方法:**
```bash
python -m sequential_live_follower.main config.json
```

**ワークフロー:**
1. アプリケーション起動（GUI表示）
2. 'N'キー押下 → 最初の楽章読み込み
3. マイクに演奏を入力
4. アプリが自動的に現在小節と同期
5. 指定小節でキーボードコマンド送信（スライド送進）
6. 'N'キー再度押下 → 次楽章読み込み

**主要機能:**
- リアルタイム音声キャプチャ（44.1kHz、モノラル）
- Chroma特徴抽出（librosa）
- DTW ベース beat 追跡（pymatchmaker）
- 信頼度ベース自動補外（低信頼度時もスムーズに再生継続）
- 小節精度トリガー実行
- ライブGUI（信頼度インジケータ付き）
- クールダウンシステム（誤発火防止）

## インストール

### 1. 依存パッケージをインストール

```bash
pip install -r requirements.txt
```

Linux/Mac では PyAudio にシステムライブラリが必要:
```bash
# Ubuntu
sudo apt-get install portaudio19-dev

# Mac
brew install portaudio
```

### 2. MusicXMLファイルを準備

- フルオーケストラスコアを `work/inbox/` に配置
- compressor.py を実行してガイド楽譜を生成

### 3. 設定ファイルを作成

```bash
cp sequential_live_follower/config_example.json config.json
# → guide_mv1.xml などのパスとトリガー設定を編集
```

## クイックスタート

```bash
# 1. スコアを圧縮
python compressor.py

# 2. config.json を編集
nano config.json

# 3. アプリケーション起動
python -m sequential_live_follower.main config.json

# 4. GUI で 'N'キーを押して最初の楽章を読み込み
# 5. マイクに演奏を入力
# 6. スライドが自動的に進む
```

## 設定ファイル

### compressor_config.json（オプション）

パート選定方法を制御:

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

### config.json（必須）

アプリケーション動作とトリガーを定義:

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
        { "measure": 1, "action": "right", "note": "楽章1 開始" },
        { "measure": 45, "action": "right", "note": "テーマA" }
      ]
    }
  ]
}
```

## システム要件

- **Python**: 3.10以上
- **CPU**: マルチコア推奨（リアルタイム特徴抽出用）
- **メモリ**: 1GB以上
- **オーディオ**: マイク入力デバイス
- **ディスプレイ**: GUI表示用
- **OS**: Linux、macOS、Windows（Linux/Mac では PortAudio 開発ライブラリ必須）

## アーキテクチャ

### スレッドモデル

すべてのリアルタイム操作がバックグラウンドスレッドで実行され、GUI がブロックされません:

- **AudioCapturer** (daemon thread): マイクフレームを連続読み込み
- **FeatureExtractor** (daemon thread): オーディオをバッファリング、Chroma 特徴抽出
- **MatchingEngine** (暗黙): 各特徴フレームに対して DTW 実行
- **TriggerExecutor** (daemon thread): トリガー監視、アクション実行
- **GUI メインループ** (tkinter): ポーリングで状態を更新、表示を刷新（ノンブロッキング）

### キュー基盤設計

コンポーネント間でスレッドセーフキューを使用してデータ競争を防止:

```
audio_queue (オーディオフレーム) → feature_queue (Chroma) → state_manager → GUI
                                                            → TriggerExecutor
```

### エラーハンドリング・堅牢性

- 各スレッドのトップレベルに try-catch
- マッチング失敗時は慣性モードで再生継続
- クールダウンシステムでトリガー誤発火防止
- PyAudio/PyMatcher なしでモック実装を使用可能

## トラブルシューティング

### オーディオ入力がない

- マイクが動作しているか確認: `python -c "import pyaudio; print(pyaudio.PyAudio().get_device_count())"`
- システム設定でマイクがデフォルト入力デバイスになっているか確認

### 常時「慣性モード」表示

- オーディオ品質が悪いか楽譜とズレている
- 静かな環境で再度試行
- ガイド楽譜が実際に演奏されている曲と一致しているか確認

### トリガーが実行されない

- config.json の小節番号が実際の楽譜と一致しているか確認
- PowerPoint がアクティブ・フォーカス中か確認
- キーボード入力が動作するか テスト: `python -c "import pyautogui; pyautogui.press('right')"`

### GUI が表示されない

- X11 フォワーディング有効か確認（リモート接続時）
- tkinter がインストールされているか: `python -c "import tkinter; print('OK')"`

## パフォーマンスプロファイリング

CPU 使用率を監視:
```bash
python -m sequential_live_follower.main config.json -v  # 詳細ログ出力
# → ログで遅いコンポーネントを確認
```

特徴抽出が通常モダン PC の 20-50% CPU を使用。マッチングは 5-10%。

## 今後の拡張

- [ ] MIDI 入力対応（オーディオ代替）
- [ ] Web UI（Tkinter 代替）
- [ ] 波形可視化
- [ ] Beat グリッド表示
- [ ] 自動信頼度適応クールダウン
- [ ] マルチ楽器対応（セクション別 DTW）
- [ ] 演奏後トリガーログ分析

## リファレンス

- **PyMatcher**: 音楽アライメント用 DTW アルゴリズム
- **Partitura**: MusicXML 構造解析
- **Music21**: 音楽記譜法ライブラリ
- **Librosa**: オーディオ特徴抽出

## ライセンス

[未定]

## サポート

問題や質問は、リポジトリで Issue を開いてください。
