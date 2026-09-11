# Terminal demo for the README hero — render with:
#
#   vhs assets/demo.tape
#
# (install: https://github.com/charmbracelet/vhs — needs ttyd + ffmpeg).
# Run from a repository checkout. The recording is the OFFLINE demo only:
# no runtime, no login, no credentials, no network beyond the local machine.
# After rendering, uncomment the GIF embed in README.md.

Output assets/demo.gif

Set Shell "bash"
Set FontSize 20
Set Width 960
Set Height 540
Set Theme {"name": "Catppuccin Mocha"}
Set Padding 24

Type@60ms "dual-agent --version"
Enter@800ms
Sleep 2s

Type@60ms "python examples/offline_mock_run.py"
Enter@500ms
Sleep 8s
