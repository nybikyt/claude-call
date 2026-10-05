"""Voice calls with Claude Code.

  call [--from-user] [--name=session] [greeting]
                                 place the call, say the greeting and wait for the first phrase;
                                 the name tells the user which session is on the line
  say [--no-wait] [--important]  speak the text (stdin or arguments) and wait for the reply
  listen [--wait=seconds]        wait for a phrase without speaking; --wait=0 returns only what was already said
  mute [on|off]                  turn the user's microphone off or on
  hangup                         hang up once both the voice and the user have finished
  key <Groq key>                 save the key: Whisper recognizes speech with it, Google without it
"""
import collections
import html
import io
import itertools
import json
import os
import queue
import re
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
SESSION_ID = os.environ.get('CLAUDE_CODE_SESSION_ID', 'default')
CALLS_DIRECTORY = HERE / 'calls'
CALL_FILE = CALLS_DIRECTORY / f'{SESSION_ID}.json'
SETTINGS_FILE = HERE / 'settings.json'
DUCKED_VOLUMES_FILE = HERE / 'ducked.json'
LOG_FILE = Path(tempfile.gettempdir()) / 'claude-call.log'
ANOTHER_CALL = 'another call'

DEFAULT_LANGUAGE = 'en'
LANGUAGES = {
    'en': {
        'recognition': 'en-US',
        'voice_file': 'voice_en.pt',
        'voice_url': 'https://models.silero.ai/models/tts/en/v3_en.pt',
        'fast_voice': 'en_0',
        'system_voice': ('409', 'David'),
        'conjunctions': {'that', 'but', 'because', 'if', 'when', 'although', 'while', 'which', 'so', 'unless'},
        'whisper_silence_phrases': {'thanks for watching', 'thank you for watching'},
    },
    'ru': {
        'recognition': 'ru-RU',
        'voice_file': 'voice_ru.pt',
        'voice_url': 'https://models.silero.ai/models/tts/ru/v4_ru.pt',
        'fast_voice': 'eugene',
        'system_voice': ('419', 'Pavel'),
        'conjunctions': {'что', 'чтобы', 'а', 'но', 'если', 'когда', 'потому', 'поэтому', 'хотя', 'пока', 'зато',
                         'однако', 'где', 'который', 'которая', 'которое', 'которые'},
        'whisper_silence_phrases': {'продолжение следует', 'спасибо за просмотр', 'субтитры сделал dimatorzok',
                                    'субтитры создавал dimatorzok',
                                    'редактор субтитров а.семкин корректор а.егорова'},
    },
}
DETECTOR_FILE = 'vad.onnx'
DETECTOR_URL = 'https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx'
SYSTEM_VOICES_KEY = 'HKEY_LOCAL_MACHINE/SOFTWARE/Microsoft/Speech_OneCore/Voices'.replace('/', os.sep)

SILENCE_ENDING_UTTERANCE = 0.8
SPEECH_STARTING_UTTERANCE = 0.16
SPEECH_PROBABILITY = 0.35
CONFIDENT_SPEECH_PROBABILITY = 0.7
CONFIDENT_SPEECH_BLOCKS = 4
LONGEST_UTTERANCE = 30
ECHO_TAIL = 0.3
ECHO_MEMORY = 8
ECHO_LEARNING_BLOCKS = 25
ECHO_DELAY_BLOCKS = 12
ECHO_LEAD_IN_BLOCKS = 3
ECHO_REFERENCE_FLOOR = 0.005
ECHO_PERCENTILE = 95
ECHO_MARGIN = 1.5
COMMA_PAUSE_RANGE = (0.1, 0.25)
HEARD_CHIME = 'Speech Off.wav'
INCOMING_RINGTONE = 'Ring05.wav'
DUCKED_VOLUME = 0.2

RING_TIMEOUT = 60
REPLY_TIMEOUT = 540
HANGUP_TIMEOUT = 45
CLAUDE_SILENCE_LIMIT = 900
REPLY_FLUSH_SECONDS = 2

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 512
BLOCK_SECONDS = BLOCK_SAMPLES / SAMPLE_RATE
PLAYBACK_RATE = 48000
PLAYBACK_BLOCK_SAMPLES = round(PLAYBACK_RATE * BLOCK_SECONDS)
GOOGLE_SPEECH_URL = ('https://www.google.com/speech-api/v2/recognize?client=chromium&pFilter=0'
                     '&key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw&lang=')
WHISPER_API_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
WHISPER_MODEL = 'whisper-large-v3-turbo'

speech_queue = queue.Queue()
played_sentences = collections.deque(maxlen=3)
recorded_utterances = queue.Queue()
heard_events = queue.Queue()
ringing_finished = threading.Event()
announcer_speaking = threading.Event()
announcer_interrupted = threading.Event()
user_speaking = threading.Event()
voice_ready = threading.Event()
call_state = 'ringing'
view = {'status': '', 'live': '', 'muted': False, 'locked': False}
settings = {'barge_in': True, 'fast': True, 'language': DEFAULT_LANGUAGE, 'zoom': 1.0}
if SETTINGS_FILE.exists():
    settings.update(json.loads(SETTINGS_FILE.read_text()))
if settings['language'] not in LANGUAGES:
    settings['language'] = DEFAULT_LANGUAGE
start_time = last_command_time = time.monotonic()
token = session_name = ''


def save_setting(name, value):
    settings[name] = value
    stored = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    stored[name] = value
    SETTINGS_FILE.write_text(json.dumps(stored))


def language():
    return LANGUAGES[settings['language']]


def set_language(code):
    save_setting('language', code)
    heard_events.put({'language': code})


def download_model(name, url):
    path = HERE / name
    if not path.exists():
        print(f'[downloading {name}]', flush=True)
        partial = path.with_suffix('.part')
        urllib.request.urlretrieve(url, partial)
        partial.replace(path)


def play_system_sound(name, loop=False):
    import winsound
    flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP * loop
    winsound.PlaySound(str(Path(os.environ['SystemRoot'], 'Media', name)), flags)


def play_dial_tone():
    import numpy as np
    import sounddevice as sd
    moment = np.arange(PLAYBACK_RATE * 3) / PLAYBACK_RATE
    fade = np.clip(np.minimum(moment, 1 - moment) / 0.1, 0, 1)
    sd.play((0.05 * np.sin(2 * np.pi * 425 * moment) * fade).astype(np.float32), PLAYBACK_RATE, loop=True)


def stop_ringing():
    import winsound
    winsound.PlaySound(None, winsound.SND_PURGE)
    if 'sounddevice' in sys.modules:
        sys.modules['sounddevice'].stop()


def duck_other_apps(on):
    try:
        from pycaw.pycaw import AudioUtilities
        saved = json.loads(DUCKED_VOLUMES_FILE.read_text()) if DUCKED_VOLUMES_FILE.exists() else {}
        sessions = [session for session in AudioUtilities.GetAllSessions()
                    if session.Identifier and session.ProcessId not in (0, os.getpid())]
        if on:
            for session in sessions:
                saved.setdefault(session.Identifier, session.SimpleAudioVolume.GetMasterVolume())
            DUCKED_VOLUMES_FILE.write_text(json.dumps(saved))
        for session in sessions:
            if session.Identifier in saved:
                volume = saved[session.Identifier] * (DUCKED_VOLUME if on else 1)
                session.SimpleAudioVolume.SetMasterVolume(volume, None)
        if not on:
            DUCKED_VOLUMES_FILE.unlink(missing_ok=True)
    except Exception:
        traceback.print_exc()


def answer():
    global call_state
    if call_state == 'ringing':
        call_state = 'talking'
        view['status'] = 'listening'
        stop_ringing()
        ringing_finished.set()


def finish(reason=''):
    global call_state
    if call_state == 'ended':
        return
    call_state = 'ended'
    stop_ringing()
    heard_events.put({'hangup': True, 'reason': reason})
    ringing_finished.set()


def take_heard(timeout):
    events = []
    try:
        events.append(heard_events.get(timeout=timeout))
        while True:
            events.append(heard_events.get_nowait())
    except queue.Empty:
        return events


def wait_for_answer(body):
    ringing_finished.wait(RING_TIMEOUT)
    if call_state != 'talking':
        finish()
    return {'answered': call_state == 'talking', 'language': settings['language']}


def say(body):
    text, wait, important = body.get('text'), body.get('wait', True), bool(body.get('important'))
    already_heard = take_heard(0) if wait and not important else []
    if text and not already_heard:
        speech_queue.put({'text': text, 'important': important})
    patience = min(body.get('patience', REPLY_TIMEOUT), REPLY_TIMEOUT)
    events = already_heard or (take_heard(patience) if wait else [])
    return {'events': events, 'unspoken': bool(text and already_heard)}


def set_mute(body):
    view['muted'] = bool(body.get('muted'))
    return {'muted': view['muted']}


def hang_up(body):
    if body.get('now'):
        finish(body.get('reason', ''))
        return {'hung_up': True, 'events': []}
    deadline = time.monotonic() + HANGUP_TIMEOUT
    while ((speech_queue.unfinished_tasks or user_speaking.is_set() or recorded_utterances.unfinished_tasks)
           and call_state == 'talking' and time.monotonic() < deadline):
        time.sleep(0.1)
    events = take_heard(0)
    if events and call_state == 'talking':
        return {'hung_up': False, 'events': events}
    finish()
    return {'hung_up': True, 'events': []}


COMMANDS = {'/answered': wait_for_answer, '/say': say, '/hangup': hang_up, '/mute': set_mute}


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global last_command_time
        url = urlparse(self.path)
        given_token = parse_qs(url.query).get('t', [''])[0]
        if not secrets.compare_digest(given_token.encode(), token.encode()):
            return self.send_error(403)
        if url.path not in COMMANDS:
            return self.send_error(404)
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        last_command_time = time.monotonic()
        reply = json.dumps(COMMANDS[url.path](body)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def start_bridge():
    global token, session_name
    call = json.loads(CALL_FILE.read_text())
    token, session_name = call['token'], call['name']
    server = ThreadingHTTPServer(('127.0.0.1', 0), BridgeHandler)
    CALL_FILE.write_text(json.dumps({**call, 'port': server.server_address[1]}))
    threading.Thread(target=server.serve_forever, daemon=True).start()


def watchdog():
    while True:
        time.sleep(5)
        now = time.monotonic()
        if call_state == 'ended':
            time.sleep(10)
            os._exit(0)
        if (call_state == 'ringing' and now - start_time > RING_TIMEOUT + 15
                or now - last_command_time > CLAUDE_SILENCE_LIMIT):
            finish()


def speaker():
    import numpy as np
    import sounddevice as sd

    fast_voices, system_voices = {}, {}

    def stopped():
        return announcer_interrupted.is_set() or call_state != 'talking'

    def fast_voice(code):
        if code not in fast_voices:
            import torch
            torch.set_num_threads(4)
            download_model(LANGUAGES[code]['voice_file'], LANGUAGES[code]['voice_url'])
            package = torch.package.PackageImporter(str(HERE / LANGUAGES[code]['voice_file']))
            model = package.load_pickle('tts_models', 'model')
            model.to(torch.device('cpu'))
            fast_voices[code] = torch, model
        return fast_voices[code]

    def say_with_fast_voice(sentence, code):
        torch, model = fast_voice(code)
        marked = html.escape(sentence).replace(',', ',<break time="200ms"/>')
        with torch.jit.optimized_execution(False), torch.inference_mode():
            audio = model.apply_tts(ssml_text=f'<speak>{marked}</speak>', speaker=LANGUAGES[code]['fast_voice'],
                                    sample_rate=PLAYBACK_RATE)
        samples = audio.numpy()
        blocks = len(samples) // PLAYBACK_BLOCK_SAMPLES
        loudness_by_block = np.abs(samples[:blocks * PLAYBACK_BLOCK_SAMPLES]).reshape(blocks, PLAYBACK_BLOCK_SAMPLES)
        envelope = loudness_by_block.mean(axis=1)
        played_sentences.append((time.monotonic(), envelope))
        sd.play(samples, PLAYBACK_RATE)
        while sd.get_stream().active and not stopped():
            time.sleep(0.02)
        sd.stop()

    def system_voice(code):
        if code not in system_voices:
            import comtypes.client
            comtypes.CoInitialize()
            voice = comtypes.client.CreateObject('SAPI.SpVoice', dynamic=True)
            modern_voices = comtypes.client.CreateObject('SAPI.SpObjectTokenCategory', dynamic=True)
            modern_voices.SetId(SYSTEM_VOICES_KEY, False)
            installed = [voices.Item(index)
                         for voices in (modern_voices.EnumerateTokens(), voice.GetVoices())
                         for index in range(voices.Count)]
            language_id, preferred_name = LANGUAGES[code]['system_voice']
            in_language = [candidate for candidate in installed
                           if language_id in candidate.GetAttribute('Language').split(';')]
            preferred = [candidate for candidate in in_language if preferred_name in candidate.GetDescription()]
            if in_language:
                voice.Voice = (preferred or in_language)[0]
            voice.Rate = 2
            system_voices[code] = voice
        return system_voices[code]

    def say_with_system_voice(sentence, code):
        speak_async, purge = 1, 2
        voice = system_voice(code)
        played_sentences.append((time.monotonic(), None))
        voice.Speak(sentence, speak_async)
        while not voice.WaitUntilDone(20) and not stopped():
            pass
        voice.Speak('', speak_async | purge)

    if settings['fast']:
        fast_voice(settings['language'])
    else:
        system_voice(settings['language'])
    voice_ready.set()

    while True:
        job = speech_queue.get()
        ringing_finished.wait()
        text = re.sub(r'[*_`#]', '', job['text'])
        sentences = [part for part in re.split(r'(?<=[.!?…])\s+', text) if part.strip()]
        announcer_interrupted.clear()
        if user_speaking.is_set() and not job['important']:
            announcer_interrupted.set()
        view['locked'] = job['important']
        announcer_speaking.set()
        try:
            for sentence in sentences:
                if stopped():
                    break
                view['status'], view['live'] = 'speaking', text
                if settings['fast']:
                    say_with_fast_voice(sentence, settings['language'])
                else:
                    say_with_system_voice(sentence, settings['language'])
        except Exception as error:
            heard_events.put({'error': f'the voice failed: {error!r}'})
        announcer_speaking.clear()
        view['locked'] = False
        speech_queue.task_done()
        if announcer_interrupted.is_set():
            while not speech_queue.empty():
                speech_queue.get_nowait()
                speech_queue.task_done()
        elif speech_queue.empty():
            view['status'] = 'listening'


def listener():
    import numpy as np
    import onnxruntime
    import sounddevice as sd

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    detector = onnxruntime.InferenceSession(str(HERE / DETECTOR_FILE), options, providers=['CPUExecutionProvider'])
    rate = np.array(SAMPLE_RATE, np.int64)
    blank = np.zeros((2, 1, 128), np.float32), np.zeros((1, 64), np.float32)
    memory, context = blank

    blocks = queue.Queue()
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=BLOCK_SAMPLES,
                            callback=lambda data, *_: blocks.put(data.copy()))
    stream.start()
    lead_in = collections.deque(maxlen=16)
    recent_pauses = collections.deque(maxlen=40)
    echo_strengths = collections.deque(maxlen=round(ECHO_MEMORY / BLOCK_SECONDS))
    recent_strengths = collections.deque(maxlen=3)
    echo_without_reference = False
    echo_until = 0
    utterance, speech_time, silence_time, pauses = None, 0, 0, []
    spoken_blocks, confident_blocks, hush_time, loudness = 0, 0, 0, 0

    def announcer_loudness(now):
        loudest = 0.0
        for started, envelope in list(played_sentences):
            if envelope is None:
                return None
            position = int((now - started) / BLOCK_SECONDS)
            heard_now = envelope[max(position - ECHO_DELAY_BLOCKS, 0):max(position + 2, 0)]
            if len(heard_now):
                loudest = max(loudest, float(heard_now.max()))
        return loudest

    def send_utterance():
        if confident_blocks < CONFIDENT_SPEECH_BLOCKS:
            view['status'] = 'listening'
            return
        comma_positions = [place / spoken_blocks for place in comma_pauses(pauses, recent_pauses)]
        audio = b''.join(part.tobytes() for part in utterance)
        recorded_utterances.put((audio, comma_positions, announcer_interrupted.is_set()))
        announcer_interrupted.clear()
        view['status'] = 'recognizing'

    while True:
        block = blocks.get()
        if announcer_speaking.is_set():
            echo_until = time.monotonic() + ECHO_TAIL
        hearing_echo = time.monotonic() < echo_until
        deaf = (call_state != 'talking' or view['muted']
                or hearing_echo and (view['locked'] or not settings['barge_in']))
        if deaf:
            if utterance is not None and view['muted'] and call_state == 'talking':
                send_utterance()
            utterance, speech_time = None, 0
            lead_in.clear()
            user_speaking.clear()
            memory, context = blank
            continue
        samples = np.concatenate([context, block.reshape(1, -1).astype(np.float32) / 32768], axis=1)
        probability, memory = detector.run(None, {'input': samples, 'state': memory, 'sr': rate})
        context = samples[:, -64:]
        is_speech = probability[0, 0] > SPEECH_PROBABILITY
        level = float(np.abs(samples[0, 64:]).mean())
        louder_than_echo = True
        if hearing_echo:
            # ponytail: there is no real echo cancellation. The microphone compares its level with what
            # the voice is saying right now and remembers the usual ratio over the last seconds, which
            # is the echo from the loudspeakers. Only a sound that stays above it counts as speech.
            # A quiet remark over a loud voice will not pass, and for the first second of a call,
            # before the echo has been measured, the voice cannot be interrupted. Use a proper AEC
            # if that stops being enough.
            announcer_level = announcer_loudness(time.monotonic())
            if (announcer_level is None) != echo_without_reference:
                echo_without_reference = announcer_level is None
                echo_strengths.clear()
            strength = level if announcer_level is None else level / (announcer_level + ECHO_REFERENCE_FLOOR)
            recent_strengths.append(strength)
            usual_echo = np.percentile(echo_strengths, ECHO_PERCENTILE) if echo_strengths else 0
            louder_than_echo = (len(echo_strengths) >= ECHO_LEARNING_BLOCKS
                                and statistics.median(recent_strengths) > ECHO_MARGIN * usual_echo)
            if utterance is None and not louder_than_echo:
                echo_strengths.append(strength)
                while len(lead_in) > ECHO_LEAD_IN_BLOCKS:
                    lead_in.popleft()
        if utterance is None:
            is_speech = is_speech and louder_than_echo
            lead_in.append(block)
            speech_time = speech_time + BLOCK_SECONDS if is_speech else 0
            if speech_time >= SPEECH_STARTING_UTTERANCE:
                utterance, silence_time, pauses = list(lead_in), 0, []
                spoken_blocks, hush_time, loudness = round(SPEECH_STARTING_UTTERANCE / BLOCK_SECONDS), 0, level
                confident_blocks = 0
                if hearing_echo:
                    print(f'voice over the announcer: strength {statistics.median(recent_strengths):.2f}, '
                          f'usual echo {usual_echo:.2f}', file=sys.stderr, flush=True)
                user_speaking.set()
                view['status'] = 'hearing'
            continue
        utterance.append(block)
        if probability[0, 0] > CONFIDENT_SPEECH_PROBABILITY and louder_than_echo:
            confident_blocks += 1
            if confident_blocks == CONFIDENT_SPEECH_BLOCKS and announcer_speaking.is_set():
                announcer_interrupted.set()
        if is_speech and level > loudness * 0.2:
            if hush_time >= COMMA_PAUSE_RANGE[0]:
                pauses.append((spoken_blocks, hush_time))
            hush_time = 0
            spoken_blocks += 1
            loudness += (level - loudness) * 0.1
        else:
            hush_time += BLOCK_SECONDS
        silence_time = 0 if is_speech else silence_time + BLOCK_SECONDS
        if silence_time >= SILENCE_ENDING_UTTERANCE or len(utterance) * BLOCK_SECONDS >= LONGEST_UTTERANCE:
            send_utterance()
            utterance, speech_time = None, 0
            lead_in.clear()
            user_speaking.clear()


def comma_pauses(pauses, recent_pauses):
    recent_pauses.extend(length for _, length in pauses)
    if not recent_pauses:
        return []
    shortest, longest = COMMA_PAUSE_RANGE
    threshold = min(longest, max(shortest, 0.6 * statistics.median(recent_pauses)))
    return [place for place, length in pauses if length >= threshold]


def recognize_with_whisper(audio, key):
    recording = io.BytesIO()
    with wave.open(recording, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio)
    boundary = secrets.token_hex(16)
    fields = {'model': WHISPER_MODEL, 'language': settings['language'], 'response_format': 'json',
              'temperature': '0'}
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                 'Content-Type: audio/wav\r\n\r\n'.encode() + recording.getvalue()
                 + f'\r\n--{boundary}--\r\n'.encode())
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': f'multipart/form-data; boundary={boundary}',
               'User-Agent': 'claude-call'}
    with urllib.request.urlopen(urllib.request.Request(WHISPER_API_URL, b''.join(parts), headers),
                                timeout=15) as response:
        text = json.load(response)['text'].strip()
    # ponytail: on silence and noise Whisper confidently returns the same subtitle phrases, and its
    # own confidence scores do not tell them from speech. The speech detector keeps noise out, and
    # this list is the backstop for noise that slips through anyway.
    return '' if text.strip(' .…!').lower() in language()['whisper_silence_phrases'] else text


def recognize_with_google(audio):
    # ponytail: an unofficial Google endpoint with the shared key from the SpeechRecognition library.
    # It needs no sign-up, but nobody guarantees its limits, it adds no punctuation and it bends rare
    # words and abbreviations toward dictionary ones. With a Groq key Whisper does the recognizing
    # and this is only the fallback.
    request = urllib.request.Request(GOOGLE_SPEECH_URL + language()['recognition'], audio,
                                     {'Content-Type': f'audio/l16; rate={SAMPLE_RATE}'})
    with urllib.request.urlopen(request, timeout=15) as response:
        results = [result for line in response.read().decode().splitlines()
                   for result in json.loads(line or '{}').get('result', [])]
    return ' '.join(result['alternative'][0].get('transcript', '').strip()
                    for result in results if result.get('alternative')).strip()


def transcribe(audio, comma_positions):
    key = os.environ.get('GROQ_API_KEY') or settings.get('groq_key')
    if key:
        try:
            return recognize_with_whisper(audio, key)
        except OSError as error:
            print(f'Whisper did not answer ({error}), falling back to Google', file=sys.stderr, flush=True)
    return with_commas(recognize_with_google(audio), comma_positions)


def with_commas(text, comma_positions):
    # ponytail: a comma goes where the speaker paused, and its place in the text is found by the
    # share of letters spoken, as if speech ran at an even pace. On a drawn-out word the comma can
    # land one word off. Real punctuation comes with a recognizer that returns it (Whisper).
    words = text.split()
    letters_spoken = list(itertools.accumulate(map(len, words)))

    def word_before_comma(position):
        guess = min(range(len(words) - 1), key=lambda index: abs(letters_spoken[index] / letters_spoken[-1] - position))
        return next((index for index in (guess, guess - 1, guess + 1)
                     if 0 <= index < len(words) - 1 and words[index + 1].lower() in language()['conjunctions']), guess)

    marked = {word_before_comma(position) for position in comma_positions if len(words) > 1}
    return ' '.join(word + ',' * (index in marked) for index, word in enumerate(words))


def recognizer():
    while True:
        audio, comma_positions, interrupted = recorded_utterances.get()
        try:
            text = transcribe(audio, comma_positions)
            print(f'utterance {len(audio) / 2 / SAMPLE_RATE:.1f} s: {text!r}', file=sys.stderr, flush=True)
            if text or interrupted:
                heard_events.put({'text': text, 'interrupted': interrupted})
                view['status'], view['live'] = 'thinking', text
                play_system_sound(HEARD_CHIME)
            elif not announcer_speaking.is_set():
                view['status'] = 'listening'
        except OSError as error:
            heard_events.put({'error': f'speech recognition failed: {error}'})
        recorded_utterances.task_done()


def report_crash(job):
    try:
        job()
    except Exception as error:
        traceback.print_exc()
        heard_events.put({'error': f'{job.__name__}: {error!r}'})


def serve(from_user):
    start_bridge()
    for job in (watchdog, speaker, listener, recognizer):
        threading.Thread(target=report_crash, args=(job,), daemon=True).start()
    if from_user:
        play_dial_tone()
    else:
        play_system_sound(INCOMING_RINGTONE, loop=True)
    import window
    window.run(sys.modules[__name__], from_user)
    finish()
    time.sleep(REPLY_FLUSH_SECONDS)
    if CALL_FILE.exists() and json.loads(CALL_FILE.read_text())['token'] == token:
        CALL_FILE.unlink()
    os._exit(0)


def request(path, body=None, timeout=REPLY_TIMEOUT + 30, call_file=None):
    call = json.loads((call_file or CALL_FILE).read_text())
    if 'port' not in call:
        raise ConnectionError('the call is still starting')
    url = f"http://127.0.0.1:{call['port']}{path}?t={call['token']}"
    without_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with without_proxy.open(urllib.request.Request(url, json.dumps(body or {}).encode()), timeout=timeout) as response:
        return json.load(response)


def end_other_calls():
    CALLS_DIRECTORY.mkdir(exist_ok=True)
    for call_file in CALLS_DIRECTORY.glob('*.json'):
        try:
            request('/hangup', {'now': True, 'reason': ANOTHER_CALL}, timeout=2, call_file=call_file)
        except (OSError, ValueError):
            call_file.unlink(missing_ok=True)


def start_call(from_user, greeting, name):
    download_model(DETECTOR_FILE, DETECTOR_URL)
    if settings['fast']:
        download_model(language()['voice_file'], language()['voice_url'])
    end_other_calls()
    CALL_FILE.write_text(json.dumps({'token': secrets.token_urlsafe(16), 'name': name or Path.cwd().name}))
    detached = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == 'nt' else {'start_new_session': True})
    command = [sys.executable, __file__, 'serve', *(['--from-user'] if from_user else [])]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=open(LOG_FILE, 'w'), **detached)
    deadline = time.monotonic() + 10
    while True:
        try:
            request('/say', {'text': greeting, 'wait': False})
            break
        except (OSError, ValueError):
            if time.monotonic() > deadline:
                sys.exit(f'[the call did not start, see {LOG_FILE}]')
            time.sleep(0.2)
    answered = request('/answered')
    if not answered['answered']:
        return print('[no answer]')
    print(f"[connected, language: {answered['language']}]")
    print_events(request('/say', {'text': ''}))


def print_events(result, waited=True):
    if result.get('unspoken'):
        print('[not spoken: the user spoke first - answer again with their words in mind]')
    for event in result['events']:
        if event.get('hangup'):
            print('[the call ended: the user started a call with another session]'
                  if event.get('reason') == ANOTHER_CALL else '[the user hung up]')
        elif event.get('error'):
            print(f"[error: {event['error']}]")
        elif event.get('language'):
            print(f"[the user switched the call language to {event['language']} - speak it from now on]")
        else:
            print(('[interrupted] ' if event['interrupted'] else '') + (event['text'] or '(unintelligible)'))
    if not result['events']:
        print('[silence]' if waited else '[speaking]')


def main():
    command, *arguments = sys.argv[1:] or ['']
    flags = {argument for argument in arguments if argument.startswith('--')}
    words = ' '.join(argument for argument in arguments if argument not in flags)
    if command == 'serve':
        return serve('--from-user' in flags)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    try:
        if command == 'call':
            name = next((flag.partition('=')[2] for flag in flags if flag.startswith('--name=')), '')
            start_call('--from-user' in flags, words, name)
        elif command in ('say', 'listen'):
            wait = '--no-wait' not in flags
            text = '' if command == 'listen' else words or sys.stdin.read()
            body = {'text': text.strip(), 'wait': wait, 'important': '--important' in flags}
            body.update({'patience': float(flag.partition('=')[2]) for flag in flags if flag.startswith('--wait=')})
            print_events(request('/say', body), wait)
        elif command == 'mute':
            muted = request('/mute', {'muted': words != 'off'})['muted']
            print("[the user's microphone is off]" if muted else "[the user's microphone is on]")
        elif command == 'key':
            save_setting('groq_key', words)
            print('[Groq key saved, Whisper recognizes speech from the next call on]')
        elif command == 'hangup':
            result = request('/hangup')
            if result['hung_up']:
                print('[call ended]')
            else:
                print('[not hung up: the user was still talking - answer them]')
                print_events(result)
        else:
            print(__doc__)
    except (OSError, ValueError):
        sys.exit('[no active call - start with call]')


if __name__ == '__main__':
    main()
