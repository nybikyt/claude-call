---
name: claude-call
description: A voice call between the user and Claude - a small window, the user talks out loud, their words arrive in the chat, a voice reads the answers, and the voice can be interrupted. Use it when the user types /claude-call or asks to call, to talk by voice, to get on a call (in any language, for example "позвони", "зв", "созвонимся"). Claude may also call on its own, without being asked - when a long task is finished, when it needs the user's decision to continue, or when something important happened.
allowed-tools: Bash(python ~/.claude/skills/claude-call/call.py *)
---

# claude-call

The call runs through `call.py` next to this file. Run every command with the Bash tool, not PowerShell: there `~` is not expanded and heredocs break.

## Speed comes first

The user waits for every answer in silence and gets annoyed by pauses. So during a call:

- a phrase arrived - answer at once with a single `say`, with no deliberation and no text in the chat;
- answer in one or two short sentences, details only when asked;
- if you need tools - first `say --no-wait` ("one second, looking"), then the work, then the result with a normal `say`.

## How to run a call

1. Place the call (timeout 600000). Pass the greeting as an argument: it is spoken the moment the call connects, and the command returns the user's first phrase.

   ```bash
   python ~/.claude/skills/claude-call/call.py call --from-user --name="Shop backend" "Hi! I'm listening."
   ```

   Use this when the user typed `/claude-call` or asked for a call: the window says the user is calling Claude, a dial tone plays, and it connects by itself in a few seconds.

   ```bash
   python ~/.claude/skills/claude-call/call.py call --name="Shop backend" "Hi! The build is done and I have a question."
   ```

   Use this when you call on your own initiative: the window says Claude is calling, a ringtone plays, there are Answer and Decline buttons, and other programs play quieter while it rings.

   `--name` is two or three words that tell the user which session is on the line: the project or the task you are working on here. The window shows it under "Claude". Use the same name for every call from this session.

   The command returns `[connected, language: en]` and the first phrase, or `[no answer]`. If there is no answer, write in the chat what you were calling about and stop. On the very first run it downloads two models first, which takes about a minute.

2. The language in `[connected, language: ...]` is the language of the call: the window, the recognition and the voice are all set to it. Speak that language, greeting included. If you do not know it before calling, greet in the language the user writes in. When `[the user switched the call language to ...]` arrives, switch with it.

3. From then on, speak and listen with one command (timeout 600000). Pass the text through a heredoc with `'EOF'` quoted:

   ```bash
   python ~/.claude/skills/claude-call/call.py say <<'EOF'
   Sure, doing it now.
   EOF
   ```

   The command speaks the text, waits for the user's phrase and prints it. That is their next message, so answer it with the next `say`. Repeat.

4. Flags of `say`:
   - `--no-wait` - speak and do not wait for a reply (before long work and for goodbyes);
   - `--important` - the user cannot interrupt, and the microphone button in the window turns red and crossed out. Only for what really matters: a warning before an irreversible action, something that must not be missed. You can also use it to finish a thought when the user keeps interrupting: start with "wait, let me finish". Do not use it for ordinary answers.

5. To say goodbye, send the last sentence with `say --no-wait` and follow it right away with `python ~/.claude/skills/claude-call/call.py hangup`. The command waits until the voice has finished and the user is silent. If they managed to say something, the call stays up and their words come back to you: answer them. Do not say goodbye with a plain `say`, because it waits for a reply while the user waits for you to hang up.

## Working and talking at the same time

The user wants to talk to you while you work, the way a message can be sent mid-task in a normal chat. There are two ways, pick one by the task.

The first way is a helper in the background. Start the task through Agent (it runs in the background) or Bash with `run_in_background`, stay on the line yourself and listen in short stretches:

```bash
python ~/.claude/skills/claude-call/call.py listen --wait=20
```

This way you hear the user at once. When they say something, answer with `say --wait=20`. When the notification arrives that the task is finished, check the result and tell them the outcome by voice. Pick this way when the task can be described in full and handed off: a search through the project, a build, tests, a large edit with a clear plan, anything that takes longer than half a minute.

The second way is to work yourself and check between steps. Between tool calls run `listen --wait=0`: it returns right away with whatever the user said in the meantime, or `[silence]`. The answer comes a few seconds late. Pick this way when the edit is short, when every step depends on the previous one and cannot be handed to a helper, or when you are going through code together and the user wants to steer you as you go.

Either way, say by voice that you have started (`say --no-wait`), and at the end say what came of it. If the user changes the task in the middle, stop the background one and start the new one instead of finishing the old one. Clean up temporary files after yourself.

`python ~/.claude/skills/claude-call/call.py mute` turns the user's microphone off, `mute off` turns it back on. Turn it off only when the user asks, and do not forget to turn it on when they ask.

## Several sessions

Every Claude Code session has its own call, and the user talks to one session at a time. When they start a call with another session, yours ends and you get `[the call ended: the user started a call with another session]`. That is not a dropped call: do not call back, just write a short summary in the chat if there was something to summarize.

The user can also ask you by voice to put them through to another session ("connect me to the session about the shop"). Then:

1. Find it with `mcp__ccd_session_mgmt__list_sessions` (load the tool through ToolSearch if it is deferred). If several sessions fit, ask which one. If the tool is not available here, say so and suggest typing `/claude-call` in that session.
2. Send it a message with `mcp__ccd_session_mgmt__send_message`: the user is on a voice call and asked to be put through to you, so call them now with the claude-call skill, and add what they wanted to discuss.
3. Tell the user by voice that the other session is about to call (`say --no-wait`) and hang up. The other session's call ends yours anyway.

If a message from another session says the user asked to be put through to you, call them right away with `call` (the ringing kind, without `--from-user`) and open with which session you are.

## What comes back

| Output | What it means |
|---|---|
| plain text | the user's phrase. With a Groq key saved the punctuation is real. Without one there are only commas, and they stand where the user paused, not where grammar wants them. Where the meaning is ambiguous, ask |
| `[interrupted] text` | the user spoke over the voice and your answer was cut off. Do not repeat it, answer the new thing |
| `[interrupted] (unintelligible)` | the voice was cut off and no words could be made out. Ask briefly |
| `[not spoken: ...]` + text | while you were thinking the user said something else. Your answer was not spoken, so answer again with the new words in mind |
| `[not hung up: ...]` + text | you called `hangup` while the user was still talking. Answer them |
| `[the user switched the call language to ...]` | the user pressed the language switch in the window. Speak that language from now on |
| `[silence]` | nothing for nine minutes. Run `call.py listen` and keep waiting |
| `[error: ...]` | the microphone, the recognition or the voice failed. If it repeats, hang up and write in the chat what broke |
| `[the call ended: the user started a call with another session]` | the user moved on to another session. Do not call back |
| `[the user hung up]` | the call is over. Do not call `say` again; write a short summary in the chat if there was something to summarize |
| `[no active call ...]` | there is no call process. Call again with `call` if the conversation is not finished |

## Writing text for the voice

- The neural voice reads only the letters of the call language: Latin letters in English, Cyrillic in Russian. It silently skips digits and the other alphabet. Write numbers as words ("twenty five", not "25") and spell foreign words in the call language's letters (in Russian: "Клод", "коммит", "пуш").
- The text is heard, not read. Short spoken sentences. No markup, lists, code, paths or links out loud: put code and long details in the chat and say by voice that you wrote them there.
- In a Russian call the recognizer hears English words and abbreviations poorly ("MIT" turns into "it"). If a phrase contains a stray scrap of Latin, work it out from context or ask.
- The user's phrases are their ordinary messages: you can do tasks, run tools and edit files.
- Recognition makes mistakes. Ask again about nonsense. Before an irreversible action (deleting, pushing, sending) repeat by voice what you understood and wait for a clear yes.

How it works, the settings and the limitations are in `README.md`. The error log is in `%TEMP%\claude-call.log`, and it also shows what the microphone heard.
