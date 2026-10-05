# claude-call

Voice calls with Claude Code. Type `/claude-call`, a small window opens, you talk into the microphone and Claude answers out loud in the same session where your project is open. You can interrupt it mid-sentence. Claude can also call you on its own, for example when a long task is done or it needs a decision from you.

This is an unofficial project. Claude and the Claude logo belong to Anthropic, and the author is not affiliated with the company.

It runs on Windows 10 and 11. Calls are in English by default; an EN / RU switch in the window changes the labels, the speech recognition and the voice together.

## What a call is like

The window shows who is calling whom: "Calling Claude…" when you started the call, and "Claude is calling you" with a ringtone when Claude did. While an incoming call is ringing, other programs play quieter, and their volume comes back once you answer.

The microphone listens all the time, so there is nothing to hold down. A speech detector tells a voice apart from keyboard clatter and other noise, so a random sound will not cut Claude off. A phrase counts as finished after 0.8 seconds of silence.

Start talking over the voice and it stops, and Claude gets your words marked as an interruption. For important messages Claude has a mode where it cannot be interrupted, and the microphone button turns red and crossed out while that lasts.

Claude cannot hang up while you are still talking. The command waits for the end of your phrase and hands your words back to it.

The window has two switches. "Fast voice" picks the voice: the neural one sounds nicer but holds about 300 MB of memory, while the Windows system voice takes almost none. "Interrupt by voice" can be turned off if the voice keeps cutting itself off through loudspeakers, and then the microphone stays silent while Claude speaks.

Drag a window edge to make it smaller or larger; everything inside scales with it and the size is remembered. EN / RU in the top right corner switches the call language, and Claude is told to switch with you.

## Installation

You need Claude Code and Python 3.10 or newer.

```
git clone https://github.com/nybikyt/claude-call %USERPROFILE%\.claude\skills\claude-call
python -m pip install -r %USERPROFILE%\.claude\skills\claude-call\requirements.txt
```

The models are not in the repository. On the first call the script downloads two of them: the Silero voice for the chosen language (57 MB for English, 38 MB for Russian) and the Silero VAD speech detector (2 MB). A CPU build of torch is enough, no graphics card is needed.

After that the `/claude-call` command shows up in Claude Code.

### Speech recognition

Out of the box Google recognizes the speech. It works right away, but it adds no punctuation and bends abbreviations and rare words toward dictionary ones.

Whisper through Groq is the better option: it writes what was actually said and punctuates it. The key is free, you can get one at [console.groq.com/keys](https://console.groq.com/keys). Save it with:

```
python %USERPROFILE%\.claude\skills\claude-call\call.py key gsk_your_key
```

The key goes into `settings.json` next to the script, and that file is not tracked by git. You can set the `GROQ_API_KEY` environment variable instead. If Groq does not answer, Google recognizes the phrase.

## How it works

One background process runs for the length of the call. It holds the window, the microphone, the speech detector, the voice and a small HTTP server on `127.0.0.1:8765`. Claude drives the call with `call.py` commands, which talk to that server using a one-time token:

| Command | What it does |
|---|---|
| `call [--from-user] [greeting]` | places the call, says the greeting and returns the first phrase |
| `say [--no-wait] [--important]` | speaks the text and waits for the reply |
| `listen [--wait=seconds]` | waits for a phrase without saying anything; with `--wait=0` it returns what has already been said |
| `mute [on\|off]` | turns the user's microphone off or on |
| `hangup` | hangs up once both sides have finished talking |
| `key <key>` | saves the Groq key |

While Claude is busy with a long task it runs the task in the background and keeps listening in short stretches through `listen --wait`, so you can talk to it in the middle of the work.

After the call the process exits on its own and the memory is freed. During a call it takes about 350 MB with the neural voice and about 60 MB with the system one.

Recognition happens in the cloud (Whisper through Groq, or Google), nothing is computed locally for it. The voice and the speech detector run on the CPU.

| File | What is in it |
|---|---|
| `call.py` | commands, server, microphone, recognition, voice |
| `window.py` | the window; icons are drawn from outlines stored in the code |
| `SKILL.md` | instructions for Claude on how to run a call |
| `test_call.py` | checks for the server and the comma placement, without a window or sound |

## Settings

Constants at the top of `call.py`:

| Name | What it changes |
|---|---|
| `DEFAULT_LANGUAGE` | the call language until the user picks another one in the window |
| `LANGUAGES` | per language: recognition locale, the neural voice file and speaker (`en_0` to `en_117` for English; `aidar`, `eugene`, `baya`, `kseniya`, `xenia` for Russian), the preferred Windows system voice |
| `SILENCE_ENDING_UTTERANCE` | how many seconds of silence end a phrase |
| `SPEECH_STARTING_UTTERANCE` | how many seconds of continuous speech it takes for the microphone to pick it up |
| `SPEECH_PROBABILITY` | how sure the detector must be to treat a sound as speech |
| `CONFIDENT_SPEECH_BLOCKS` | how many blocks of confident speech it takes for a phrase to be recognized and to interrupt the voice |
| `ECHO_MARGIN` | how far above the voice's own echo a sound must be to count as you |
| `COMMA_PAUSE_RANGE` | the range of pause lengths that produce a comma |
| `HEARD_CHIME` | the "heard you, thinking" sound from `C:\Windows\Media` |
| `DUCKED_VOLUME` | how far other programs are turned down during an incoming call |

## Limitations

There is no real echo cancellation. The microphone compares what it hears with what the voice is saying at that moment and remembers the strongest ratio over the last few seconds, which is the echo from the loudspeakers. Only a sound that stays above that ratio counts as speech. A normal voice is enough to interrupt Claude, but the first word sometimes gets clipped, a very quiet remark over a loud voice will not get through, and during the first second of a call, before the echo has been measured, you cannot interrupt at all. With the Windows system voice there is nothing to compare against, so a plain loudness threshold is used.

The fallback recognition goes through an unofficial Google endpoint with the shared key from the [SpeechRecognition](https://github.com/Uberi/speech_recognition) library. Nobody promises any limits there, and it handles English words and abbreviations poorly.

Without a Groq key, commas follow pauses rather than grammar. The position of a pause in the text is estimated from the share of letters spoken, so on a drawn-out word the comma sometimes lands one word off.

The neural voice reads only the letters of its own language and skips digits and the other alphabet. That is why Claude writes numbers out in words.

Response time is bounded by the model itself: the voice and the recognition fit in about a second, and the rest is Claude thinking.

## Tests

```
python test_call.py
```

## License

The code is released under the MIT license, see `LICENSE`.

The phone and microphone icons come from Google's [Material Symbols](https://github.com/google/material-design-icons), licensed under Apache 2.0.

The Silero models are downloaded separately and come under their own licenses: [silero-models](https://github.com/snakers4/silero-models) and [silero-vad](https://github.com/snakers4/silero-vad).
