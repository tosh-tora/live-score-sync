# Sequential Live Follower - 日本語ガイド

リアルタイムオーケストラ性能追従システム。マイク入力を解析し、MusicXML楽譜と照合し、指定小節でPowerPointスライドを自動制御します。

## 主要機能

- **リアルタイム音声解析**: マイク入力を取得してChroma特徴を抽出
- **DTW基づくアライメント**: PyMatcherで演奏と楽譜を照合
- **小節精度トリガー**: 指定小節で自動的にキーボードコマンド送信
- **追従ロックインゲート**: 信頼度低下時は直前の確信できた拍位置を保持（テンポ外挿はしない — pymatchmaker の DTW 自身の復帰を待つ）
- **シーケンシャル楽章読み込み**: 'N'キーで次の楽章を順次読み込み
- **ライブ表示GUI**: 現在小節、信頼度、次トリガーを大画面表示

## システムアーキテクチャ

### スレッドモデル

```
メインスレッド (tkinter GUI)
├─ 画面更新（リアルタイム表示）
└─ バックグラウンドワーカー:
   ├─ AudioCapturer (PyAudio ストリーム → audio_queue)
   ├─ FeatureExtractor (audio_queue → Chroma → feature_queue)
   ├─ MatchingEngine (feature_queue → DTW → beat/measure更新)
   ├─ TriggerExecutor (トリガー実行 → pyautogui)
   └─ KeyboardListener (グローバル 'N'キー)
```

### コアコンポーネント

| モジュール | 目的 |
|----------|------|
| `score_mapper.py` | Beat ↔ 小節 変換（変拍子対応） |
| `audio_capturer.py` | リアルタイムマイクキャプチャ (PyAudio) |
| `feature_extractor.py` | Chroma特徴抽出 (librosa) |
| `matcher.py` | PyMatcher DTW統合 |
| `state_manager.py` | スレッドセーフ中央状態管理 |
| `inertia_engine.py` | 追従ロックインゲート（低信頼度時は直前拍を保持・外挿はしない） |
| `cooldown_timer.py` | トリガー連発防止（3秒クールダウン） |
| `gui_tkinter.py` | Tkinter リアルタイムGUI |
| `config/loader.py` | JSON設定ファイル解析 |
| `main.py` | アプリケーションオーケストレータ |

## インストール

### 前提条件

- Python 3.10以上
- MusicXMLファイル （compressor.py から生成）

### セットアップ

```bash
# 依存パッケージをインストール
pip install -r requirements.txt

# Linux/Mac では PyAudio に追加のシステムライブラリが必要:
# Ubuntu: sudo apt-get install portaudio19-dev
# Mac: brew install portaudio
```

## 設定

### compressor_config.json（オプション）

フルスコアからパートを選定する方法を制御:

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

- **note_count**: 音符が多いパートを優先
- **rhythmic_resolution**: より細かい音価を持つパートを優先（16分音符 > 4分音符）
- **pitch_variance**: 音高変動が大きいパートを優先
- **top_n**: 各小節で選定するパート数

### config.json（必須）

アプリケーション動作とスライドトリガーを制御:

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

## 使用方法

```bash
# アプリケーション起動
python -m sequential_live_follower.main config.json

# 詳細ログ出力
python -m sequential_live_follower.main config.json -v
```

### ワークフロー

1. **アプリケーション起動**: GUI が開く、最初の楽章待機状態
2. **'N'キー押下**: config.json から最初の楽章を読み込み、音声キャプチャ開始
3. **演奏**: マイクに演奏または歌唱を入力
4. **自動同期**: アプリが自動的に現在小節と同期
5. **トリガー実行**: 指定小節到達時、キーボードコマンド送信（例: 'right' = スライド送進）
6. **次楽章**: 'N'キー再度押下で次楽章読み込み
7. **終了**: GUI ウィンドウを閉じるか Ctrl+C

### ディスプレイ表示

GUI に表示される内容:
- **ファイル名**: 現在の MusicXML ファイル
- **大型小節表示**: 現在の小節（1からのカウント）
- **信頼度バー**: DTW アライメント品質（緑=高、赤=低）
- **次トリガー**: 次のトリガー小節
- **[慣性モード]**: 互換のため残存（現バージョンでは常に非表示）
- **クールダウン表示**: トリガー猶予期間中に表示

## 動作原理

### Beat 追跡（リアルタイムフロー）

1. **音声キャプチャ**: AudioCapturer がマイクを読み込み → audio_queue
2. **特徴抽出**: FeatureExtractor がフレームをバッファリング、librosa で Chroma 12次元ベクトル抽出 → feature_queue
3. **マッチング**: MatchingEngine が PyMatcher DTW でChroma を楽譜と照合 → (beat, confidence)
4. **追従ホールド**: confidence < 閾値の場合、直前の確信できた beat をそのまま保持（外挿はしない）
5. **小節変換**: ScoreMapper が beat を小節番号に変換（累積beat↔小節マップ使用）
6. **状態更新**: AppState が (beat, measure, confidence) を更新
7. **GUI 表示**: GUI が 100ms ごとにポーリング、表示を更新
8. **トリガー判定**: TriggerExecutor が現在小節にトリガーがあるか確認、クールダウン確認
9. **アクション実行**: PyAutoGUI がキーボードコマンド送信（例: 'right'）

### ScoreMapper（重要コンポーネント）

変拍子に対応したcumulative beat マップを初期化時に構築:

```
小節1 (4/4): beat 0.0–4.0
小節2 (3/4): beat 4.0–7.0
小節3 (5/8): beat 7.0–9.25
...
```

二分探索により beat→小節 変換が O(log n) で実行。

### 追従ロックインゲート

confidence が閾値以下（デフォルト 0.4）に低下した場合:
- ロックイン前は beat=0 を返し、トリガー発火を抑止する
- ロックイン後は **直前の確信できた beat を保持** し続ける
- テンポによる外挿は行わない（pymatchmaker の DTW が音声品質回復で自分で復帰する設計）

旧バージョンでは「直前テンポで beat を外挿」していたが、ライブ演奏の表現的揺らぎで confidence が頻繁に落ち、外挿が指揮者より先走る → タイムアウトで measure=1 にリセット、という症状を起こすため撤去した。

## トラブルシューティング

### 音声キャプチャの問題

- **音声入力なし**: システム設定でマイクが選択されているか確認
- **ノイズ/ドロップアウト**: chunk_size を減らすか feature_queue サイズを縮小
- **高遅延**: chroma_hop_length を増加

### マッチングの問題

- **拍が常に直前値で止まったまま**: confidence が常に閾値以下 → 音質が悪いか楽譜とズレ。静かな環境で再度試行、楽譜確認、`confidence_threshold` を下げる
- **小節間でジャンプ**: beat リセット可能性 → 楽譜内の大きいテンポ変更を確認
- **トリガーが実行されない**: config.json の小節番号を確認、PowerPoint がアクティブか確認

### キーボード入力

- **'N'キー反応なし**: keyboard ライブラリをインストールするか、代替入力方法を使用
- **スライド進行しない**: PowerPoint がフォーカス中か、キーボードコマンド受け付けか確認

## パフォーマンス注記

- **CPU 使用率**: 特徴抽出（librosa）が最も高負荷。必要に応じて chroma_hop_length を減らす
- **遅延**: 総遅延 ~200-400ms（スライド制御に許容範囲内）
- **メモリ**: 適度。キューサイズに上限あり、無制限増加なし

## API 概要

### セッション開始

```python
from sequential_live_follower.main import SequentialFollower

app = SequentialFollower("config.json")
app.run()  # GUI クローズまでブロック
```

### 高度な使用（プログラマティックアクセス）

```python
from sequential_live_follower.core.score_mapper import ScoreMapper
from sequential_live_follower.core.inertia_engine import InertiaEngine

# Beat を小節に変換
mapper = ScoreMapper("guide_mv1.xml")
measure = mapper.beat_to_measure(45.5)

# 信頼度ゲート経由で beat を取得（外挿はしない — 低信頼度時は直前値を保持）
gate = InertiaEngine(confidence_threshold=0.4)
beat, _inertia_unused, tempo = gate.update(current_beat=45.0, confidence=0.3)
```

## 依存パッケージ

- **music21**: MusicXML 解析（compressor.py、テスト用）
- **pymatchmaker**: DTW ベース beat 追跡
- **partitura**: MusicXML 構造解析（ScoreMapper 用）
- **librosa**: オーディオ特徴抽出（Chroma）
- **pyaudio**: マイクキャプチャ
- **pyautogui**: キーボード制御
- **keyboard**: グローバルホットキー対応（オプション）
- **numpy**: 数値演算
- **tkinter**: GUI（通常 Python に付属）

## 今後の拡張

- [ ] 波形可視化 GUI
- [ ] Beat グリッド表示
- [ ] マッチャー失敗時の手動テンポ調整
- [ ] 全トリガー記録（演奏後の分析用）
- [ ] 複数デバイスオーディオ入力選択
- [ ] Web UI（Tkinter の代替）
- [ ] MIDI ノート入力対応（オーディオ代替）
- [ ] 信頼度適応型クールダウン（短縮 for 高信頼度）

## ライセンス

[未定]

## サポート

問題、質問、プルリクエストは GitHub リポジトリで受け付けます。
