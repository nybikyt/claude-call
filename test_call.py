import collections
import tempfile
import threading
import urllib.error
from pathlib import Path

import call

call.PORT = 8799
call.TOKEN_FILE = Path(tempfile.gettempdir()) / 'claude-call-test.token'
call.TOKEN_FILE.write_text('test-token')
call.stop_ringing = lambda: None
call.start_bridge()


def later(action, *arguments):
    threading.Timer(0.3, action, arguments).start()


call.TOKEN_FILE.write_text('wrong')
try:
    call.request('/say')
    raise AssertionError('чужой токен прошёл')
except urllib.error.HTTPError as error:
    assert error.code == 403
call.TOKEN_FILE.write_text('test-token')

later(call.answer)
assert call.request('/answered') == {'answered': True}

later(call.heard_events.put, {'text': 'как дела', 'interrupted': True})
assert call.request('/say', {'text': 'привет'}) == {
    'events': [{'text': 'как дела', 'interrupted': True}], 'unspoken': False}
assert call.speech_queue.get_nowait() == {'text': 'привет', 'important': False}
call.speech_queue.task_done()

call.heard_events.put({'text': 'и ещё', 'interrupted': False})
stale_answer = call.request('/say', {'text': 'устаревший ответ'})
assert stale_answer['unspoken'] and stale_answer['events'][0]['text'] == 'и ещё'
assert call.speech_queue.empty()

call.heard_events.put({'text': 'погоди', 'interrupted': False})
important_answer = call.request('/say', {'text': 'важное', 'important': True})
assert not important_answer['unspoken'] and important_answer['events'][0]['text'] == 'погоди'
assert call.speech_queue.get_nowait() == {'text': 'важное', 'important': True}
call.speech_queue.task_done()

assert call.request('/say', {'text': 'секунду', 'wait': False})['events'] == []
assert call.speech_queue.get_nowait()['text'] == 'секунду'
call.speech_queue.task_done()

call.heard_events.put({'text': 'подожди я не договорил', 'interrupted': False})
assert call.request('/hangup') == {
    'hung_up': False, 'events': [{'text': 'подожди я не договорил', 'interrupted': False}]}
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

assert call.comma_pauses([(10, 0.128)], collections.deque()) == [10]
assert call.comma_pauses([(5, 0.128), (20, 0.32)], collections.deque([0.3] * 10)) == [20]
assert call.comma_pauses([], collections.deque()) == []

print('ok')
