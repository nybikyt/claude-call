"""Голосовой звонок с Claude Code.

  call [--from-user] [привет]    позвонить, поздороваться и дождаться первой реплики
  say [--no-wait] [--important]  озвучить текст (stdin или аргументы) и дождаться реплики
  listen                         дождаться реплики, ничего не говоря
  hangup                         положить трубку, когда договорят и диктор, и пользователь
"""
import collections
import html
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
SPEECH_STARTING_UTTERANCE = 0.2
SPEECH_PROBABILITY = 0.5
LONGEST_UTTERANCE = 30
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
SPEECH_API_URL = ('https://www.google.com/speech-api/v2/recognize?client=chromium'
                  f'&lang={RECOGNITION_LANGUAGE}&key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw')

speech_queue = queue.Queue()
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
    events = already_heard or (take_heard(REPLY_TIMEOUT) if wait else [])
    return {'events': events, 'unspoken': bool(text and already_heard)}


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


COMMANDS = {'/answered': wait_for_answer, '/say': say, '/hangup': hang_up}


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
        sd.play(audio.numpy(), PLAYBACK_RATE)
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
    utterance, speech_time, silence_time, pauses = None, 0, 0, []
    spoken_blocks, hush_time, loudness = 0, 0, 0
    while True:
        block = blocks.get()
        # ponytail: эхоподавления нет. С колонками диктор попадает в микрофон и перебивает сам себя,
        # поэтому перебивание отключается переключателем в окне. Настоящий AEC нужен, если
        # понадобится перебивать и без наушников.
        deaf = (call_state != 'talking' or view['muted']
                or announcer_speaking.is_set() and (view['locked'] or not settings['barge_in']))
        if deaf:
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
            lead_in.append(block)
            speech_time = speech_time + BLOCK_SECONDS if is_speech else 0
            if speech_time >= SPEECH_STARTING_UTTERANCE:
                utterance, silence_time, pauses = list(lead_in), 0, []
                spoken_blocks, hush_time, loudness = round(SPEECH_STARTING_UTTERANCE / BLOCK_SECONDS), 0, level
                user_speaking.set()
                if announcer_speaking.is_set():
                    announcer_interrupted.set()
                view['status'] = 'слушаю…'
            continue
        utterance.append(block)
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
            comma_positions = [place / spoken_blocks for place in comma_pauses(pauses, recent_pauses)]
            audio = b''.join(part.tobytes() for part in utterance)
            recorded_utterances.put((audio, comma_positions, announcer_interrupted.is_set()))
            announcer_interrupted.clear()
            utterance, speech_time = None, 0
            lead_in.clear()
            user_speaking.clear()
            view['status'] = 'распознаю…'


def comma_pauses(pauses, recent_pauses):
    recent_pauses.extend(length for _, length in pauses)
    if not recent_pauses:
        return []
    shortest, longest = COMMA_PAUSE_RANGE
    threshold = min(longest, max(shortest, 0.6 * statistics.median(recent_pauses)))
    return [place for place, length in pauses if length >= threshold]


def recognize(audio):
    # ponytail: неофициальный адрес Google с общим ключом из библиотеки SpeechRecognition. Ничего не
    # считает локально, но лимиты не гарантированы и знаков препинания нет. Замена: Whisper
    # (локально нужен torch с CUDA) или платный API с личным ключом.
    request = urllib.request.Request(SPEECH_API_URL, audio, {'Content-Type': f'audio/l16; rate={SAMPLE_RATE}'})
    with urllib.request.urlopen(request, timeout=15) as response:
        results = [result for line in response.read().decode().splitlines()
                   for result in json.loads(line or '{}').get('result', [])]
    return ' '.join(result['alternative'][0].get('transcript', '').strip()
                    for result in results if result.get('alternative')).strip()


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
            text = with_commas(recognize(audio), comma_positions)
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
            print_events(request('/say', {'text': text.strip(), 'wait': wait, 'important': '--important' in flags}),
                         wait)
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
