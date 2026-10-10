What each person gets and what they hand back

Lane 1, the laptop side (capture, tray, hotkey).
- Gets: the screen, the name of the app in front, and a running local server on port 8000 to talk to.
- Hands back: a screenshot plus "which app, which window, what time" sent to that local server every time the screen changes. A double-click launcher that starts everything and opens the dashboard. A little popup on a hotkey where you type a question and see the answer with thumbnails.

Lane 2, the dashboard.
- Gets: a chat endpoint that streams an answer word by word and then tells you which screenshots it used. Plus endpoints for the promises list, the timesheet button, the draft button, and the screenshot images.
- Hands back: one web page that looks like ChatGPT. Answers appear as they stream, every "[#9]" in an answer is a chip, clicking it shows the screenshot. Side panel with the promises. Two links other people can open: "show me capture 9" and "open chat with this question".

Lane 3, the brain.
- Gets: a search function over the screenshots, an activity timeline function, a list-promises function, and a "talk to Nemotron" function that supports tools.
- Hands back: the chat endpoint. Give it the conversation, it searches the screenshots, answers in plain language, and cites the exact screenshot ids it used. If it finds nothing it says so instead of guessing. Also the promise extractor that watches new screenshots and pulls out "I'll send X by Friday" lines.

Lane 4, the models and storage.
- Gets: raw screenshots coming in, questions to embed, chat requests to forward to the GX10.
- Hands back: the GX10 reachable from the laptop (right now it only answers itself, that's the first job). The Python functions everyone else calls: describe a screenshot, embed text, chat with tools, transcribe audio. The fake versions of those so people can work without the GX10. The database and search.
  

How it chains: Lane 1 drops sand stores them → Lane 3answers questions from that s the answer.
