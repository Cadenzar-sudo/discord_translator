Translator Overlay — quick start
================================

This package is pre-configured to connect to the translation server.
You only need steps 1-3.

1. Unzip this folder anywhere (keep the files together).
2. Double-click TranslatorOverlay.exe.
   - Windows SmartScreen may warn the first time (the exe is unsigned).
     Click "More info" -> "Run anyway".
3. Done! Subtitles appear at the bottom of your screen whenever people
   talk in the voice channel. The window is click-through — it never
   blocks your game.

Hotkeys
-------
   Ctrl+Alt+P   pause / resume subtitles
   Ctrl+Alt+O   show / hide the overlay
   Ctrl+Alt+I   toggle click-through (to reach the tray menu)
   Ctrl+Alt+G   switch Chat mode <-> Game mode (server owner only)
                (chat = more accurate, game = faster short callouts)
   Ctrl+Alt+L   cycle subtitle language (server owner only): AUTO ->
                Korean -> Japanese -> Vietnamese
   Ctrl+Alt+Q   quit

There is also a tray icon (あA) with the same options.

The overlay is pre-set to show English subtitles (Korean/Japanese speech
translated to English). If the server owner tells you your language
changed, run "TranslatorOverlay.exe --setup" in a terminal and paste the
setup code from step 3 of this file again — you'll get the language menu.

If it ever says "reconnecting..." the server is down or your internet
dropped - it retries by itself, nothing to do.

Everyone in the voice channel is being transcribed so you can read each
other - by running this you agree to that for yourself too. :)
