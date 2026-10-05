import collections
import tempfile
import threading
import urllib.error
from pathlib import Path

import call

call.PORT = 8799
call.TOKEN_FILE = Path(tempfile.gettempdir()) / 'claude-call-test.token'
call.SETTINGS_FILE = Path(tempfile.gettempdir()) / 'claude-call-test-settings.json'
call.SETTINGS_FILE.unlink(missing_ok=True)
call.TOKEN_FILE.write_text('test-token')
call.stop_ringing = lambda: None
call.start_bridge()


def later(action, *arguments):
    threading.Timer(0.3, action, arguments).start()


call.TOKEN_FILE.write_text('wrong')
try:
    call.request('/say')
    raise AssertionError('a wrong token was accepted')
except urllib.error.HTTPError as error:
    assert error.code == 403
call.TOKEN_FILE.write_text('test-token')

call.settings['language'] = 'en'
later(call.answer)
assert call.request('/answered') == {'answered': True, 'language': 'en'}

later(call.heard_events.put, {'text': 'how are you', 'interrupted': True})
assert call.request('/say', {'text': 'hello'}) == {
    'events': [{'text': 'how are you', 'interrupted': True}], 'unspoken': False}
assert call.speech_queue.get_nowait() == {'text': 'hello', 'important': False}
call.speech_queue.task_done()

call.heard_events.put({'text': 'one more thing', 'interrupted': False})
stale_answer = call.request('/say', {'text': 'a stale answer'})
assert stale_answer['unspoken'] and stale_answer['events'][0]['text'] == 'one more thing'
assert call.speech_queue.empty()

call.heard_events.put({'text': 'wait', 'interrupted': False})
important_answer = call.request('/say', {'text': 'important', 'important': True})
assert not important_answer['unspoken'] and important_answer['events'][0]['text'] == 'wait'
assert call.speech_queue.get_nowait() == {'text': 'important', 'important': True}
call.speech_queue.task_done()

assert call.request('/say', {'text': 'one second', 'wait': False})['events'] == []
assert call.speech_queue.get_nowait()['text'] == 'one second'
call.speech_queue.task_done()

assert call.request('/say', {'text': '', 'patience': 0})['events'] == []
assert call.request('/mute', {'muted': True}) == {'muted': True} and call.view['muted']
assert call.request('/mute', {'muted': False}) == {'muted': False} and not call.view['muted']

call.set_language('ru')
assert call.settings['language'] == 'ru' and call.language()['recognition'] == 'ru-RU'
assert call.request('/say', {'text': '', 'patience': 0})['events'] == [{'language': 'ru'}]

call.heard_events.put({'text': 'wait, I am not done', 'interrupted': False})
assert call.request('/hangup') == {
    'hung_up': False, 'events': [{'text': 'wait, I am not done', 'interrupted': False}]}
assert call.call_state == 'talking'

assert call.request('/hangup') == {'hung_up': True, 'events': []}
assert call.request('/say', {'text': ''})['events'] == [{'hangup': True}]

assert call.with_commas('Казнить нельзя помиловать', [0.30]) == 'Казнить, нельзя помиловать'
assert call.with_commas('Казнить нельзя помиловать', [0.57]) == 'Казнить нельзя, помиловать'
assert (call.with_commas('сделай кнопку потом проверь звук и перезвони', [0.35, 0.725])
        == 'сделай кнопку, потом проверь звук, и перезвони')
assert call.with_commas('я хотел чтобы всё стало тише', [0.4]) == 'я хотел, чтобы всё стало тише'
assert call.with_commas('Привет это проверка', []) == 'Привет это проверка'
assert call.with_commas('да', [0.5]) == 'да' and call.with_commas('', [0.5]) == ''

call.settings['language'] = 'en'
assert (call.with_commas('I wanted it quieter because it was loud', [0.4])
        == 'I wanted it quieter, because it was loud')

assert call.comma_pauses([(10, 0.128)], collections.deque()) == [10]
assert call.comma_pauses([(5, 0.128), (20, 0.32)], collections.deque([0.3] * 10)) == [20]
assert call.comma_pauses([], collections.deque()) == []

call.SETTINGS_FILE.unlink(missing_ok=True)
print('ok')
