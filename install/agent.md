Install and set up hold-dictate on this computer for me, check that it works, and report any problem you can't fix on GitHub.

hold-dictate is voice typing for the whole computer: hold Alt+Space, speak, let go, and the words are typed at the cursor. Site: https://hold-dictate.vercel.app. Code and issues: https://github.com/Can-0f-Tuna/hold-dictate

1. Install
   - macOS or Linux: curl -fsSL https://hold-dictate.vercel.app/install.sh | sh
   - Windows (PowerShell): irm https://hold-dictate.vercel.app/install.ps1 | iex
   The installer installs uv if it is missing, installs the latest release, starts hold-dictate now and at every login, and opens the settings page in my browser. If the hold-dictate command is not found afterwards, open a new shell or add ~/.local/bin to PATH. Run hold-dictate --version to confirm.

2. API key. I do this part myself.
   Tell me to open https://console.groq.com/keys, sign in, click Create API Key, paste the key into the settings page that just opened and click Check and save. Groq has a free tier. Don't ask me to paste the key into this chat, and never write it to a file, a command line or an issue. If the settings page didn't open, run: hold-dictate settings
   Wait until I say it's saved.

3. Check the setup
   Run: hold-dictate doctor
   Fix every line marked FIX by following the hint printed under it. Things only I can do, tell me exactly what to click:
   - macOS: allow Microphone, and Accessibility / Input Monitoring for hold-dictate (or the terminal) in System Settings > Privacy & Security.
   - Linux: when doctor says keyboard access needs the input group, run the command it prints, then I log out and back in once.
   - Windows: if SmartScreen or an antivirus asks about Python, I allow it.
   Run doctor again until it prints "All good." Then check that it is running: on macOS and Linux, hold-dictate service status should print "running". On Windows, this should list a process: Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object { $_.CommandLine -like "*hold_dictate*" }
   If it is not running, run: hold-dictate service install

4. Test transcription
   Make a short WAV of a known English sentence, for example "The quick brown fox jumps over the lazy dog":
   - macOS: say -o /tmp/hd-test.wav --data-format=LEI16@16000 "The quick brown fox jumps over the lazy dog"
   - Linux: espeak-ng -w /tmp/hd-test.wav "The quick brown fox jumps over the lazy dog" (or espeak, or pico2wave)
   - Windows (PowerShell): Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SetOutputToWaveFile("$env:TEMP\hd-test.wav"); $s.Speak("The quick brown fox jumps over the lazy dog"); $s.Dispose()
   Then run: hold-dictate transcribe <that file>
   It should print roughly the same sentence. If no speech tool is available, skip this step and say so.

5. Test the real thing with me
   Ask me to click into any text field, hold Alt+Space, say a sentence, and let go. A small pill shows at the bottom of the screen while it listens (on Linux with Wayland and quickshell; elsewhere there's a short sound). The text should appear about a second later. Ask me whether it worked.

6. If something fails that you can't fix
   Report it so the maintainers can fix it:
   a. Search existing issues first: gh issue list --repo Can-0f-Tuna/hold-dictate --state all --search "<a few words about the error>"
      If one matches, add a comment with your details (gh issue comment <number> --repo Can-0f-Tuna/hold-dictate --body-file <file>) instead of opening a new one.
   b. Otherwise write a new issue with:
      - Title: a short description of the problem, starting with [agent]
      - Operating system and version, desktop session on Linux (echo $XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP), CPU architecture
      - hold-dictate --version
      - The full output of hold-dictate doctor
      - The step that failed, the exact command, and its full output or error message
      - What you already tried
      Remove the API key and anything else private before posting. Keys look like gsk_..., sk-... or any long random string. Never attach the config file itself.
   c. Show me the issue text and post it once I agree: gh issue create --repo Can-0f-Tuna/hold-dictate --title "<title>" --body-file <file>
      If gh is not installed or not signed in, give me this link to open instead, with the title and body URL-encoded: https://github.com/Can-0f-Tuna/hold-dictate/issues/new?title=<title>&body=<body>
   d. Give me the link to the issue.

7. When you are done, tell me in a few lines: what you installed and which version, what doctor says, whether both tests passed, and any issue you opened. Mention that settings (key, hotkey, languages, model, recording pill look) are in the hold-dictate icon next to the clock or in the menu bar, or by running hold-dictate settings, and that updates install themselves.
