#!/usr/bin/env python3
"""
Algorithmic MusicXML Compressor
===============================
フルスコアから pymatchmaker が認識しやすい「ガイド譜」を自動生成する。

アルゴリズム:
1. 各小節について、全パートの「活動量スコア」を計算
2. 上位N個のパートを選定
3. 選定パートの小節をそれぞれ独立した出力パートにコピー
4. N個のパートを持つMusicXML（圧縮.mxl）として出力

使用方法:
- 単一ファイル処理: python compressor.py input.xml
- バッチ処理（全ファイル）: python compressor.py
- 出力先: work/outbox/ に自動保存（.mxl形式）
"""

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from music21 import converter, stream, note, chord, meter, key, tempo, clef, instrument


# デフォルト設定
DEFAULT_CONFIG = {
    "weights": {
        "note_count": 1.0,
        "rhythmic_resolution": 3.0,
        "pitch_variance": 0.5
    },
    "top_n": 4
}


def load_config(config_path: str | None) -> dict:
    """
    設定ファイルを読み込み、デフォルト値とマージ
    """
    config = DEFAULT_CONFIG.copy()
    config["weights"] = DEFAULT_CONFIG["weights"].copy()

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            # weights をマージ
            if "weights" in user_config:
                config["weights"].update(user_config["weights"])
            # top_n を上書き
            if "top_n" in user_config:
                config["top_n"] = user_config["top_n"]
            print(f"Config loaded: {config_path}")
        else:
            print(f"Warning: Config file not found: {config_path}, using defaults")

    return config


def discover_inbox_files(pattern: str = "*.xml") -> list[Path]:
    """
    work/inbox/ ディレクトリから XML/MXL ファイルを検出
    Returns: MusicXML ファイルのパスリスト（ソート済み）
    """
    inbox_dir = Path("work/inbox")
    if not inbox_dir.exists():
        print(f"Warning: {inbox_dir} directory not found")
        return []

    # .xml と .mxl の両方を検索
    xml_files = sorted(inbox_dir.glob("*.xml"))
    mxl_files = sorted(inbox_dir.glob("*.mxl"))
    files = sorted(xml_files + mxl_files)
    return files


def ensure_outbox_directory() -> Path:
    """
    work/outbox/ ディレクトリを作成（存在しない場合）
    Returns: outbox パス
    """
    outbox_dir = Path("work/outbox")
    outbox_dir.mkdir(parents=True, exist_ok=True)
    return outbox_dir


def get_output_path(input_filename: str, outbox_dir: Path) -> Path:
    """
    出力ファイルパスを生成
    input_filename から _guide.mxl を足したファイルをoutbox内に作成
    """
    input_path = Path(input_filename)
    output_filename = f"{input_path.stem}_guide.mxl"
    return outbox_dir / output_filename
@dataclass
class PartActivity:
    """パートの小節ごとの活動量"""
    part_id: str
    part_name: str
    note_count: int           # 音符数
    rhythmic_resolution: float  # リズム解像度（最小音価の逆数）
    pitch_variance: float      # 音高の分散（メロディ的動き）
    is_percussion: bool = False  # 打楽器パート（マッチング除外優先）

    def calculate_score(self, weights: dict) -> float:
        """
        総合スコア（重み付け）
        weights: {"note_count": 1.0, "rhythmic_resolution": 3.0, "pitch_variance": 0.5}
        """
        return (
            self.note_count * weights.get("note_count", 1.0) +
            self.rhythmic_resolution * weights.get("rhythmic_resolution", 3.0) +
            self.pitch_variance * weights.get("pitch_variance", 0.5)
        )


def list_parts(score: stream.Score) -> list[tuple[str, str]]:
    """スコア内の全パート情報を取得"""
    parts_info = []
    for i, part in enumerate(score.parts):
        part_id = part.id or f"Part_{i}"
        part_name = part.partName or part_id
        parts_info.append((part_id, part_name))
    return parts_info


def calculate_rhythmic_resolution(measure: stream.Measure) -> float:
    """
    小節内の最小音価から「リズム解像度」を計算
    quarterLength が小さいほど解像度が高い
    """
    min_duration = float('inf')

    for elem in measure.recurse().notesAndRests:
        if elem.quarterLength > 0:
            min_duration = min(min_duration, elem.quarterLength)

    if min_duration == float('inf'):
        return 0.0

    # 4分音符=1.0を基準に、解像度を返す
    # 16分音符(0.25) → 4.0, 8分音符(0.5) → 2.0, 4分音符(1.0) → 1.0
    return 1.0 / min_duration


def calculate_pitch_variance(measure: stream.Measure) -> float:
    """音高の分散を計算（メロディ的な動きの指標）"""
    pitches = []

    for elem in measure.recurse().notes:
        if isinstance(elem, note.Note):
            pitches.append(elem.pitch.midi)
        elif isinstance(elem, chord.Chord):
            # コードの場合は最高音を使用
            pitches.append(max(p.midi for p in elem.pitches))

    if len(pitches) < 2:
        return 0.0

    mean_pitch = sum(pitches) / len(pitches)
    variance = sum((p - mean_pitch) ** 2 for p in pitches) / len(pitches)
    return variance ** 0.5  # 標準偏差


def _find_active_attribute(
    source_part: stream.Part,
    measure_number: int,
    cls: type,
):
    """
    source_part 内で measure_number 番目（含む）以前の小節を順に走査し、
    最後に出現した cls 要素を返す。見つからなければ part 直下も探す。

    music21 の getContextByClass() は measure 自体に含まれる要素を返さない
    ケースがあるため、明示的に走査する。
    """
    found = None
    for m in source_part.getElementsByClass(stream.Measure):
        if m.number is None or m.number > measure_number:
            continue
        elements = list(m.getElementsByClass(cls))
        if elements:
            found = elements[-1]
    if found is None:
        part_level = list(source_part.getElementsByClass(cls))
        if part_level:
            found = part_level[0]
    return found


def _apply_source_attributes(
    new_measure: stream.Measure,
    source_part: stream.Part,
    measure_number: int,
) -> None:
    """
    元パートの楽器が切り替わった小節の先頭に、その小節時点で有効な
    clef / keySignature / instrument を明示的に挿入する。

    既存の同種要素は重複を避けるため一旦取り除いてから挿入する。
    """
    current_clef = _find_active_attribute(source_part, measure_number, clef.Clef)
    current_key = _find_active_attribute(source_part, measure_number, key.KeySignature)
    current_instrument = _find_active_attribute(source_part, measure_number, instrument.Instrument)

    for existing in list(new_measure.getElementsByClass(clef.Clef)):
        new_measure.remove(existing)
    for existing in list(new_measure.getElementsByClass(key.KeySignature)):
        new_measure.remove(existing)
    for existing in list(new_measure.getElementsByClass(instrument.Instrument)):
        new_measure.remove(existing)

    if current_instrument is not None:
        new_measure.insert(0, copy.deepcopy(current_instrument))
    if current_key is not None:
        new_measure.insert(0, copy.deepcopy(current_key))
    if current_clef is not None:
        new_measure.insert(0, copy.deepcopy(current_clef))


def _is_percussion_part(part: stream.Part) -> bool:
    """パートが音程のない打楽器パートかどうかを判定する。
    instrument クラスによる判定を優先し、フォールバックとして PercussionClef を確認する。
    """
    instr = part.getInstrument()
    if isinstance(instr, instrument.UnpitchedPercussion):
        return True
    first_clef = part.recurse().getElementsByClass(clef.Clef).first()
    return isinstance(first_clef, clef.PercussionClef)


def analyze_measure_activity(
    score: stream.Score,
    measure_number: int
) -> list[PartActivity]:
    """指定小節における各パートの活動量を分析"""
    activities = []

    for part in score.parts:
        part_id = part.id or "unknown"
        part_name = part.partName or part_id
        is_perc = _is_percussion_part(part)

        # 該当小節を取得
        measure = part.measure(measure_number)
        if measure is None:
            continue

        # 音符数をカウント
        note_count = len(list(measure.recurse().notes))

        # リズム解像度
        rhythmic_res = calculate_rhythmic_resolution(measure)

        # 音高分散
        pitch_var = calculate_pitch_variance(measure)

        activities.append(PartActivity(
            part_id=part_id,
            part_name=part_name,
            note_count=note_count,
            rhythmic_resolution=rhythmic_res,
            pitch_variance=pitch_var,
            is_percussion=is_perc,
        ))

    return activities




def compress_score(
    input_path: str,
    output_path: str,
    top_n: int = 4,
    weights: dict | None = None,
    verbose: bool = True
) -> None:
    """
    メイン処理: スコアを圧縮して top_n 個の独立したパートに分配

    Args:
        input_path: 入力MusicXMLファイルパス
        output_path: 出力MusicXMLファイルパス
        top_n: 各小節で選択するパート数（出力パート数と同じ）
        weights: 重み設定 {"note_count", "rhythmic_resolution", "pitch_variance"}
        verbose: 詳細出力
    """
    if weights is None:
        weights = DEFAULT_CONFIG["weights"]
    print(f"Loading: {input_path}")
    score = converter.parse(input_path)

    # パート一覧を表示
    parts_info = list_parts(score)
    print(f"\n=== パート一覧 ({len(parts_info)} parts) ===")
    for pid, pname in parts_info:
        print(f"  [{pid}] {pname}")

    # 小節範囲を取得
    all_measures = set()
    for part in score.parts:
        for m in part.getElementsByClass(stream.Measure):
            if m.number is not None:
                all_measures.add(m.number)

    measure_numbers = sorted(all_measures)
    if not measure_numbers:
        print("Error: No measures found in score")
        return

    print(f"\n=== 処理開始 (小節 {measure_numbers[0]}〜{measure_numbers[-1]}) ===")

    # 新しいスコアを作成
    new_score = stream.Score()

    # top_n 個の出力パートを作成
    output_parts = []
    for i in range(top_n):
        new_part = stream.Part()
        new_part.partName = f"Guide {i + 1}"
        new_part.id = f"guide_{i + 1}"
        output_parts.append(new_part)

    # 各出力パートが直前に使用した元パートID（楽器切替を検知するため）
    last_source_ids: list[str | None] = [None] * top_n

    # 各小節を処理
    for m_num in measure_numbers:
        # 活動量を分析
        activities = analyze_measure_activity(score, m_num)

        # スコア順にソート（weightsを使用）
        activities.sort(key=lambda x: x.calculate_score(weights), reverse=True)

        # 上位N個を選定（活動があるパートのみ）
        # 原則: 非打楽器を優先。非打楽器が不足する場合のみ打楽器で補完。
        # 例外: 非打楽器が1つもアクティブでない小節は打楽器を使用。
        active_parts = [a for a in activities if a.note_count > 0]
        non_perc = [a for a in active_parts if not a.is_percussion]
        perc = [a for a in active_parts if a.is_percussion]
        if non_perc:
            selected = non_perc[:top_n]
            if len(selected) < top_n:
                selected += perc[:top_n - len(selected)]
        else:
            selected = perc[:top_n]

        if verbose and m_num <= 5:  # 最初の5小節だけ詳細表示
            print(f"\n小節 {m_num}:")
            for a in selected:
                perc_mark = " [perc]" if a.is_percussion else ""
                print(f"  {a.part_name}{perc_mark}: score={a.calculate_score(weights):.1f} "
                      f"(notes={a.note_count}, res={a.rhythmic_resolution:.1f}, "
                      f"var={a.pitch_variance:.1f})")

        # 選定された各パートの小節を、対応する出力パートにコピー
        for i in range(top_n):
            if i < len(selected):
                # 選定されたパートの小節をコピー
                source_part_id = selected[i].part_id
                source_part = None
                for part in score.parts:
                    if part.id == source_part_id:
                        source_part = part
                        break

                if source_part:
                    source_measure = source_part.measure(m_num)
                    if source_measure:
                        # 小節をディープコピーして追加
                        new_measure = copy.deepcopy(source_measure)
                        new_measure.number = m_num
                        # 元パート（楽器）が前回から切り替わった場合は
                        # 新しい楽器の clef / keySignature / instrument を明示挿入する
                        if last_source_ids[i] != source_part_id:
                            _apply_source_attributes(new_measure, source_part, m_num)
                        output_parts[i].append(new_measure)
                        last_source_ids[i] = source_part_id
                    else:
                        # 元パートに該当小節がない場合は休符小節を追加
                        # （休符自体には楽器属性を付与しないため last_source_ids は更新しない）
                        rest_measure = stream.Measure(number=m_num)
                        rest_measure.append(note.Rest(quarterLength=4.0))
                        output_parts[i].append(rest_measure)
                else:
                    # パートが見つからない場合も休符小節を追加
                    rest_measure = stream.Measure(number=m_num)
                    rest_measure.append(note.Rest(quarterLength=4.0))
                    output_parts[i].append(rest_measure)
            else:
                # 選定されたパートが top_n より少ない場合は休符小節を追加
                rest_measure = stream.Measure(number=m_num)
                rest_measure.append(note.Rest(quarterLength=4.0))
                output_parts[i].append(rest_measure)

    # 全パートをスコアに追加
    for part in output_parts:
        new_score.append(part)

    # 保存
    print(f"\n=== 保存中: {output_path} ===")
    new_score.write('musicxml.mxl', fp=output_path)
    print("完了!")


def main():
    parser = argparse.ArgumentParser(
        description='MusicXML Compressor - フルスコアからガイド譜を自動生成'
    )
    parser.add_argument(
        'input',
        nargs='?',
        default=None,
        help='入力MusicXMLファイル（work/inbox内）。指定なしでバッチ処理（全ファイル）'
    )
    parser.add_argument(
        '-o', '--output',
        help='出力ファイル名（デフォルト: input_guide.mxl）。work/outbox内に保存'
    )
    parser.add_argument(
        '-n', '--top-n',
        type=int,
        default=None,
        help='各小節で選択するパート数（デフォルト: 4）'
    )
    parser.add_argument(
        '-c', '--config',
        help='設定ファイル（JSON）のパス'
    )
    parser.add_argument(
        '--weight-notes',
        type=float,
        help='音符数の重み（設定ファイルを上書き）'
    )
    parser.add_argument(
        '--weight-rhythm',
        type=float,
        help='リズム解像度の重み（設定ファイルを上書き）'
    )
    parser.add_argument(
        '--weight-pitch',
        type=float,
        help='音高分散の重み（設定ファイルを上書き）'
    )
    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='詳細出力を抑制'
    )

    args = parser.parse_args()

    # 設定ファイルを読み込み
    config = load_config(args.config)

    # CLI引数で重みを上書き
    if args.weight_notes is not None:
        config["weights"]["note_count"] = args.weight_notes
    if args.weight_rhythm is not None:
        config["weights"]["rhythmic_resolution"] = args.weight_rhythm
    if args.weight_pitch is not None:
        config["weights"]["pitch_variance"] = args.weight_pitch

    # top_n: CLI引数 > 設定ファイル > デフォルト
    top_n = args.top_n if args.top_n is not None else config["top_n"]

    # outbox ディレクトリを作成
    outbox_dir = ensure_outbox_directory()

    print(f"Weights: {config['weights']}")
    print(f"Output directory: {outbox_dir}")

    # バッチ処理 vs 単一ファイル処理
    if args.input is None:
        # バッチモード: work/inbox内の全XMLファイル
        xml_files = discover_inbox_files()
        if not xml_files:
            print("No XML/MXL files found in work/inbox/")
            return 1

        print(f"\n=== Batch Processing ({len(xml_files)} files) ===\n")
        success_count = 0
        fail_count = 0

        for xml_file in xml_files:
            try:
                output_path = get_output_path(xml_file.name, outbox_dir)
                print(f"Processing: {xml_file.name} → {output_path.name}")

                compress_score(
                    str(xml_file),
                    str(output_path),
                    top_n=top_n,
                    weights=config["weights"],
                    verbose=not args.quiet
                )
                success_count += 1
            except Exception as e:
                print(f"  ERROR: {e}")
                fail_count += 1
                continue

        print(f"\n=== Batch Summary ===")
        print(f"Succeeded: {success_count}")
        print(f"Failed: {fail_count}")
        return 0 if fail_count == 0 else 1

    else:
        # 単一ファイル処理
        # work/inbox内のファイルを検索
        inbox_dir = Path("work/inbox")
        input_file = inbox_dir / args.input

        if not input_file.exists():
            print(f"Error: File not found in {inbox_dir}: {args.input}")
            return 1

        # 出力パスの決定
        if args.output:
            output_path = outbox_dir / args.output
        else:
            output_path = get_output_path(args.input, outbox_dir)

        print(f"Input:  {input_file}")
        print(f"Output: {output_path}")

        compress_score(
            str(input_file),
            str(output_path),
            top_n=top_n,
            weights=config["weights"],
            verbose=not args.quiet
        )

        return 0


if __name__ == '__main__':
    exit(main())
