# MusicXML Orchestral Presentation Automation System

A complete system for automated slide control during live orchestral performances. Monitors real-time audio, aligns with MusicXML notation, and sends keyboard commands to advance PowerPoint slides at specified measures.

## Project Structure

```
mmatch/
├── compressor.py                      # Task 1: MusicXML Compressor
├── compressor_config.example.json     # Configuration template for compressor
├── requirements.txt                   # Python dependencies
├── work/
│   ├── inbox/                         # Input directory (full orchestral MusicXML)
│   └── outbox/                        # Output directory (compressed guide scores)
├── sequential_live_follower/          # Task 2: Main Application
│   ├── main.py                        # Entry point
│   ├── config_example.json            # Configuration template
│   ├── README.md                      # Detailed documentation
│   ├── core/
│   │   ├── audio_capturer.py          # PyAudio stream capture
│   │   ├── feature_extractor.py       # Chroma feature extraction
│   │   ├── matcher.py                 # PyMatcher DTW integration
│   │   ├── score_mapper.py            # Beat↔Measure conversion
│   │   ├── state_manager.py           # Thread-safe state
│   │   ├── inertia_engine.py          # Confidence-based fallback
│   │   ├── cooldown_timer.py          # Trigger rate limiting
│   │   └── keyboard_listener.py       # (Planned) Global hotkey
│   ├── ui/
│   │   ├── gui_tkinter.py             # Real-time display
│   │   └── layouts.py                 # (Planned) UI themes
│   ├── config/
│   │   └── loader.py                  # JSON config parser
│   └── utils/
│       ├── logger.py                  # (Planned) Logging setup
│       └── ...
├── CLAUDE.md                          # Development guidelines
└── specification.txt                  # System specification
```

## Two-Part System

### Task 1: MusicXML Compressor (Pre-processor)

**Purpose**: Extract the "musically important" parts from a full orchestral score and create a lightweight guide score optimized for real-time matching.

**Usage**:
```bash
# Single file
python compressor.py score.xml

# Batch process all files in work/inbox/
python compressor.py

# With custom weights
python compressor.py score.xml -c compressor_config.json --weight-rhythm 5.0
```

**Output**: Compressed guide scores saved to `work/outbox/`

**Key Features**:
- Automatic part selection based on "activity score" (note density, rhythmic resolution, pitch variance)
- Configurable weights for fine-tuning
- Unison deduplication (removes redundant simultaneous notes)
- Variable time signature support

### Task 2: Sequential Live Follower (Main Application)

**Purpose**: Real-time performance tracking and slide automation.

**Usage**:
```bash
python -m sequential_live_follower.main config.json
```

**Workflow**:
1. Application starts with GUI
2. Press 'N' to load first movement
3. Perform into microphone
4. Application automatically syncs to current measure
5. Keyboard commands sent at configured triggers (slide advance)
6. Press 'N' to load next movement

**Key Features**:
- Real-time audio capture (44.1kHz, mono)
- Chroma feature extraction (librosa)
- DTW-based beat tracking (pymatchmaker)
- Confidence-based fallback (inertia mode for low confidence)
- Measure-accurate trigger execution
- Live GUI with confidence indicator
- Cooldown system to prevent misfires

## Installation

1. **Clone/download this repository**

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

   On Linux/Mac, PyAudio may require system libraries:
   ```bash
   # Ubuntu
   sudo apt-get install portaudio19-dev

   # Mac
   brew install portaudio
   ```

3. **Prepare MusicXML files**:
   - Place full orchestral scores in `work/inbox/`
   - Run compressor to generate guide scores in `work/outbox/`

4. **Create configuration**:
   - Copy `sequential_live_follower/config_example.json` to `config.json`
   - Edit with your movement files and trigger settings

## Quick Start

```bash
# 1. Compress orchestral scores
python compressor.py

# 2. Create config.json (edit paths and triggers)
cp sequential_live_follower/config_example.json config.json
# ... edit config.json ...

# 3. Run application
python -m sequential_live_follower.main config.json

# 4. In GUI, press 'N' to load first movement
# 5. Start performing/playing into microphone
# 6. Slides advance automatically at configured measures
# 7. Press 'N' to load next movement
```

## Configuration

### compressor_config.json (Optional)

Controls how musical parts are selected from full scores:

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

- **note_count**: Prioritize parts with more notes
- **rhythmic_resolution**: Prioritize parts with smaller note values (16th vs quarter)
- **pitch_variance**: Prioritize parts with wider pitch range
- **top_n**: Number of parts to select per measure

### config.json (Required)

Controls application behavior and slide triggers:

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
        { "measure": 1, "action": "right", "note": "Movement 1 Start" },
        { "measure": 45, "action": "right", "note": "Theme A" }
      ]
    }
  ]
}
```

## System Requirements

- **Python**: 3.10+
- **CPU**: Multi-core recommended (for real-time feature extraction)
- **Memory**: 1GB+ (for MusicXML parsing + queues)
- **Audio**: Microphone input device
- **Display**: Display for GUI (or headless with modifications)
- **OS**: Linux, macOS, Windows (requires PortAudio on Linux/Mac)

## Architecture Notes

### Threading Model

All real-time operations run in background threads to prevent GUI freezing:

- **AudioCapturer** (daemon thread): Reads microphone frames continuously
- **FeatureExtractor** (daemon thread): Buffers audio, extracts Chroma features
- **MatchingEngine** (implicit in main loop): Runs DTW on each feature frame
- **TriggerExecutor** (daemon thread): Monitors for triggers, executes actions
- **GUI Main Loop** (tkinter): Polls state, updates display without blocking

### Queue-Based Design

Components communicate via thread-safe queues to prevent data races:

```
audio_queue (audio frames) → feature_queue (Chroma vectors) → state_manager → GUI
                                                            → TriggerExecutor
```

### Error Handling & Robustness

- Each thread has try-catch at top level
- Inertia mode maintains playback during matching failures
- Cooldown system prevents trigger misfires
- Graceful degradation (e.g., mock matcher if pymatchmaker unavailable)

## Troubleshooting

### Audio Not Captured
- Check microphone is working: `python -c "import pyaudio; print(pyaudio.PyAudio().get_device_count())"`
- Verify microphone is default input device in system settings

### Constant "INERTIA MODE"
- Audio quality poor or score mismatch
- Try re-recording in quieter environment
- Verify guide score matches performed piece

### Triggers Not Firing
- Check measure numbers in config.json match actual score
- Verify PowerPoint is in focus and active
- Test keyboard input works: `python -c "import pyautogui; pyautogui.press('right')"`

### GUI Not Displaying
- Ensure X11 forwarding enabled if remote
- Try headless mode (future enhancement)

## Development

See `CLAUDE.md` for development guidelines, workflow, and best practices.

## Testing

[Integration tests to be added - currently manual testing recommended]

## Performance Profiling

Monitor CPU usage:
```bash
python -m sequential_live_follower.main config.json -v  # Verbose logging
# Watch for slow components in logs
```

Feature extraction typically takes 20-50% CPU on modern machines; matching takes 5-10%.

## Future Enhancements

- [ ] MIDI input support (alternative to audio)
- [ ] Web UI (browser-based alternative to Tkinter)
- [ ] Waveform visualization
- [ ] Beat grid display overlay
- [ ] Automatic confidence-adaptive cooldown
- [ ] Multi-instrument support (separate DTW per section)
- [ ] Post-performance trigger logging & analysis

## References

- **PyMatcher**: DTW algorithm for music alignment
- **Partitura**: MusicXML structure analysis
- **Music21**: Music notation library
- **Librosa**: Audio feature extraction

## License

[To be determined]

## Contact

For issues or questions, please open an issue on the repository.
