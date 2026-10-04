"""Голосовой звонок с Claude Code.

  call [--from-user] [привет]    позвонить, поздороваться и дождаться первой реплики
  say [--no-wait] [--important]  озвучить текст (stdin или аргументы) и дождаться реплики
  listen [--wait=секунды]        дождаться реплики, ничего не говоря; --wait=0 - только уже сказанное
  mute [on|off]                  выключить или включить микрофон пользователя
  hangup                         положить трубку, когда договорят и диктор, и пользователь
  key <ключ Groq>                сохранить ключ: с ним распознаёт Whisper, без него Google
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
TOKEN_FILE = HERE / '.token'
SETTINGS_FILE = HERE / 'settings.json'
DUCKED_VOLUMES_FILE = HERE / 'ducked.json'
LOG_FILE = Path(tempfile.gettempdir()) / 'claude-call.log'
MODEL_URLS = {
    'voice.pt': 'https://models.silero.ai/models/tts/ru/v4_ru.pt',
    'vad.onnx': 'https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx',
}
PORT = 8765

FAST_VOICE = 'eugene'
LIGHT_VOICE = 'Pavel'
RECOGNITION_LANGUAGE = 'ru-RU'
SILENCE_ENDING_UTTERANCE = 0.8
SPEECH_STARTING_UTTERANCE = 0.16
SPEECH_PROBABILITY = 0.35
CONFIDENT_SPEECH_PROBABILITY = 0.7
CONFIDENT_SPEECH_BLOCKS = 4
LONGEST_UTTERANCE = 30
ECHO_TAIL = 0.3
ECHO_MEMORY = 3
ECHO_LEARNING_BLOCKS = 15
ECHO_DELAY_BLOCKS = 12
ECHO_LEAD_IN_BLOCKS = 3
ECHO_REFERENCE_FLOOR = 0.005
ECHO_MARGIN = 1.6
COMMA_PAUSE_RANGE = (0.1, 0.25)
CONJUNCTIONS = {'что', 'чтобы', 'а', 'но', 'если', 'когда', 'потому', 'поэтому', 'хотя', 'пока', 'зато',
                'однако', 'где', 'который', 'которая', 'которое', 'которые'}
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
SPEECH_API_URL = ('https://www.google.com/speech-api/v2/recognize?client=chromium&pFilter=0'
                  f'&lang={RECOGNITION_LANGUAGE}&key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw')
WHISPER_API_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
WHISPER_MODEL = 'whisper-large-v3-turbo'
WHISPER_SILENCE_PHRASES = {'продолжение следует', 'спасибо за просмотр', 'субтитры сделал dimatorzok',
                           'субтитры создавал dimatorzok', 'редактор субтитров а.семкин корректор а.егорова'}

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
settings = {'barge_in': True, 'fast': True}
if SETTINGS_FILE.exists():
    settings.update(json.loads(SETTINGS_FILE.read_text()))
start_time = last_command_time = time.monotonic()
token = ''


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(settings))


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
        view['status'] = 'слушаю'
        stop_ringing()
        ringing_finished.set()


def finish():
    global call_state
    if call_state == 'ended':
        return
    call_state = 'ended'
    stop_ringing()
    heard_events.put({'hangup': True})
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
    return {'answered': call_state == 'talking'}


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
        finish()
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


class BridgeServer(ThreadingHTTPServer):
    allow_reuse_address = False


def start_bridge():
    global token
    token = TOKEN_FILE.read_text()
    deadline = time.monotonic() + 5
    while True:
        try:
            server = BridgeServer(('127.0.0.1', PORT), BridgeHandler)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
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

    def stopped():
        return announcer_interrupted.is_set() or call_state != 'talking'

    def load_fast_voice():
        import torch
        torch.set_num_threads(4)
        model = torch.package.PackageImporter(str(HERE / 'voice.pt')).load_pickle('tts_models', 'model')
        model.to(torch.device('cpu'))
        return torch, model

    def say_with_fast_voice(sentence):
        torch, model = fast_voice
        marked = html.escape(sentence).replace(',', ',<break time="200ms"/>')
        with torch.jit.optimized_execution(False), torch.inference_mode():
            audio = model.apply_tts(ssml_text=f'<speak>{marked}</speak>', speaker=FAST_VOICE,
                                    sample_rate=PLAYBACK_RATE)
        samples = audio.numpy()
        blocks = len(samples) // PLAYBACK_BLOCK_SAMPLES
        envelope = np.abs(samples[:blocks * PLAYBACK_BLOCK_SAMPLES]).reshape(blocks, PLAYBACK_BLOCK_SAMPLES).mean(axis=1)
        played_sentences.append((time.monotonic(), envelope))
        sd.play(samples, PLAYBACK_RATE)
        while sd.get_stream().active and not stopped():
            time.sleep(0.02)
        sd.stop()

    def load_light_voice():
        import comtypes.client
        comtypes.CoInitialize()
        voice = comtypes.client.CreateObject('SAPI.SpVoice', dynamic=True)
        category = comtypes.client.CreateObject('SAPI.SpObjectTokenCategory', dynamic=True)
        category.SetId('HKEY_LOCAL_MACHINE/SOFTWARE/Microsoft/Speech_OneCore/Voices'.replace('/', os.sep), False)
        voices = category.EnumerateTokens()
        for index in range(voices.Count):
            if LIGHT_VOICE in voices.Item(index).GetDescription():
                voice.Voice = voices.Item(index)
        voice.Rate = 2
        return voice

    def say_with_light_voice(sentence):
        speak_async, purge = 1, 2
        played_sentences.append((time.monotonic(), None))
        light_voice.Speak(sentence, speak_async)
        while not light_voice.WaitUntilDone(20) and not stopped():
            pass
        light_voice.Speak('', speak_async | purge)

    fast_voice = light_voice = None
    if settings['fast']:
        fast_voice = load_fast_voice()
    else:
        light_voice = load_light_voice()
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
                view['status'], view['live'] = 'Claude говорит…', text
                if settings['fast']:
                    fast_voice = fast_voice or load_fast_voice()
                    say_with_fast_voice(sentence)
                else:
                    light_voice = light_voice or load_light_voice()
                    say_with_light_voice(sentence)
        except Exception as error:
            heard_events.put({'error': f'диктор не сработал: {error!r}'})
        announcer_speaking.clear()
        view['locked'] = False
        speech_queue.task_done()
        if announcer_interrupted.is_set():
            while not speech_queue.empty():
                speech_queue.get_nowait()
                speech_queue.task_done()
        elif speech_queue.empty():
            view['status'] = 'слушаю'


def listener():
    import numpy as np
    import onnxruntime
    import sounddevice as sd

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    detector = onnxruntime.InferenceSession(str(HERE / 'vad.onnx'), options, providers=['CPUExecutionProvider'])
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
    recent_strengths = collections.deque(maxlen=4)
    echo_without_reference = False
    echo_until = 0

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
    utterance, speech_time, silence_time, pauses = None, 0, 0, []
    spoken_blocks, confident_blocks, hush_time, loudness = 0, 0, 0, 0

    def send_utterance():
        if confident_blocks < CONFIDENT_SPEECH_BLOCKS:
            view['status'] = 'слушаю'
            return
        comma_positions = [place / spoken_blocks for place in comma_pauses(pauses, recent_pauses)]
        audio = b''.join(part.tobytes() for part in utterance)
        recorded_utterances.put((audio, comma_positions, announcer_interrupted.is_set()))
        announcer_interrupted.clear()
        view['status'] = 'распознаю…'

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
        if utterance is None:
            if hearing_echo:
                # ponytail: настоящего эхоподавления нет. Микрофон сравнивает громкость с тем, что
                # диктор произносит в этот момент, и запоминает обычное соотношение - это эхо из
                # колонок. Речью считается только то, что заметно его превышает. Тихий голос поверх
                # громкого диктора не пройдёт; полноценный AEC нужен, если этого станет мало.
                announcer_level = announcer_loudness(time.monotonic())
                if (announcer_level is None) != echo_without_reference:
                    echo_without_reference = announcer_level is None
                    echo_strengths.clear()
                strength = level if announcer_level is None else level / (announcer_level + ECHO_REFERENCE_FLOOR)
                recent_strengths.append(strength)
                louder_than_echo = (len(echo_strengths) >= ECHO_LEARNING_BLOCKS
                                    and max(recent_strengths) > ECHO_MARGIN * np.percentile(echo_strengths, 90))
                if not louder_than_echo:
                    echo_strengths.append(strength)
                    while len(lead_in) > ECHO_LEAD_IN_BLOCKS:
                        lead_in.popleft()
                is_speech = is_speech and louder_than_echo
            lead_in.append(block)
            speech_time = speech_time + BLOCK_SECONDS if is_speech else 0
            if speech_time >= SPEECH_STARTING_UTTERANCE:
                utterance, silence_time, pauses = list(lead_in), 0, []
                spoken_blocks, hush_time, loudness = round(SPEECH_STARTING_UTTERANCE / BLOCK_SECONDS), 0, level
                confident_blocks = 0
                user_speaking.set()
                view['status'] = 'слушаю…'
            continue
        utterance.append(block)
        if probability[0, 0] > CONFIDENT_SPEECH_PROBABILITY:
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
    fields = {'model': WHISPER_MODEL, 'language': RECOGNITION_LANGUAGE[:2], 'response_format': 'json',
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
    # ponytail: на тишине и шуме Whisper уверенно выдаёт одни и те же фразы из субтитров, а его
    # оценки уверенности их не отличают от речи. Шум отсекает детектор речи, а этот список -
    # страховка на случай, если шум всё же прошёл.
    return '' if text.strip(' .…!').lower() in WHISPER_SILENCE_PHRASES else text


def recognize_with_google(audio):
    # ponytail: неофициальный адрес Google с общим ключом из библиотеки SpeechRecognition. Работает без
    # регистрации, но лимиты не гарантированы, знаков препинания нет, а редкие слова и сокращения он
    # подгоняет под словарные. Поэтому с ключом Groq распознаёт Whisper, а это запасной путь.
    request = urllib.request.Request(SPEECH_API_URL, audio, {'Content-Type': f'audio/l16; rate={SAMPLE_RATE}'})
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
            print(f'Whisper не ответил ({error}), распознаю через Google', file=sys.stderr, flush=True)
    return with_commas(recognize_with_google(audio), comma_positions)


def with_commas(text, comma_positions):
    # ponytail: запятая ставится по паузе, а её место в тексте ищется по доле произнесённых букв,
    # будто речь идёт с ровной скоростью. На растянутом слове запятая может съехать на соседнее.
    # Настоящая пунктуация появится вместе с распознавателем, который её отдаёт (Whisper).
    words = text.split()
    letters_spoken = list(itertools.accumulate(map(len, words)))

    def word_before_comma(position):
        guess = min(range(len(words) - 1), key=lambda index: abs(letters_spoken[index] / letters_spoken[-1] - position))
        return next((index for index in (guess, guess - 1, guess + 1)
                     if 0 <= index < len(words) - 1 and words[index + 1].lower() in CONJUNCTIONS), guess)

    marked = {word_before_comma(position) for position in comma_positions if len(words) > 1}
    return ' '.join(word + ',' * (index in marked) for index, word in enumerate(words))


def recognizer():
    while True:
        audio, comma_positions, interrupted = recorded_utterances.get()
        try:
            text = transcribe(audio, comma_positions)
            print(f'реплика {len(audio) / 2 / SAMPLE_RATE:.1f} с: {text!r}', file=sys.stderr, flush=True)
            if text or interrupted:
                heard_events.put({'text': text, 'interrupted': interrupted})
                view['status'], view['live'] = 'Claude думает…', text
                play_system_sound(HEARD_CHIME)
            elif not announcer_speaking.is_set():
                view['status'] = 'слушаю'
        except OSError as error:
            heard_events.put({'error': f'распознавание не сработало: {error}'})
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
    TOKEN_FILE.unlink(missing_ok=True)
    os._exit(0)


def request(path, body=None, timeout=REPLY_TIMEOUT + 30):
    url = f'http://127.0.0.1:{PORT}{path}?t={TOKEN_FILE.read_text()}'
    without_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with without_proxy.open(urllib.request.Request(url, json.dumps(body or {}).encode()), timeout=timeout) as response:
        return json.load(response)


def download_missing_models():
    for name, url in MODEL_URLS.items():
        path = HERE / name
        if not path.exists():
            print(f'[скачиваю {name}]', flush=True)
            partial = path.with_suffix('.part')
            urllib.request.urlretrieve(url, partial)
            partial.replace(path)


def start_call(from_user, greeting):
    download_missing_models()
    try:
        request('/hangup', {'now': True}, timeout=2)
    except OSError:
        pass
    TOKEN_FILE.write_text(secrets.token_urlsafe(16))
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
        except OSError:
            if time.monotonic() > deadline:
                sys.exit(f'[звонок не запустился, см. {LOG_FILE}]')
            time.sleep(0.2)
    if not request('/answered')['answered']:
        return print('[не взял трубку]')
    print('[соединено]')
    print_events(request('/say', {'text': ''}))


def print_events(result, waited=True):
    if result.get('unspoken'):
        print('[не озвучено: пользователь заговорил раньше - ответь заново с учётом его слов]')
    for event in result['events']:
        if event.get('hangup'):
            print('[пользователь положил трубку]')
        elif event.get('error'):
            print(f"[ошибка: {event['error']}]")
        else:
            print(('[перебил] ' if event['interrupted'] else '') + (event['text'] or '(неразборчиво)'))
    if not result['events']:
        print('[тишина]' if waited else '[озвучивается]')


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
            start_call('--from-user' in flags, words)
        elif command in ('say', 'listen'):
            wait = '--no-wait' not in flags
            text = '' if command == 'listen' else words or sys.stdin.read()
            body = {'text': text.strip(), 'wait': wait, 'important': '--important' in flags}
            body.update({'patience': float(flag.partition('=')[2]) for flag in flags if flag.startswith('--wait=')})
            print_events(request('/say', body), wait)
        elif command == 'mute':
            muted = request('/mute', {'muted': words != 'off'})['muted']
            print('[микрофон пользователя выключен]' if muted else '[микрофон пользователя включён]')
        elif command == 'key':
            settings['groq_key'] = words
            save_settings()
            print('[ключ Groq сохранён, со следующего звонка распознаёт Whisper]')
        elif command == 'hangup':
            result = request('/hangup')
            if result['hung_up']:
                print('[звонок завершён]')
            else:
                print('[трубка не положена: пользователь ещё говорил - ответь ему]')
                print_events(result)
        else:
            print(__doc__)
    except OSError:
        sys.exit('[звонок не активен - начни с call]')


if __name__ == '__main__':
    main()
