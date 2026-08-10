Output docs/assets/property-inventory-demo.gif

Set Shell "bash"
Set FontSize 36
Set LetterSpacing 0
Set LineHeight 1.0
Set Width 1600
Set Height 1080
Set Padding 40
Set Margin 24
Set MarginFill "#0d1117"
Set BorderRadius 16
Set WindowBar "Rings"
Set WindowBarSize 42
Set Theme { "black": "#000000", "red": "#f78166", "green": "#56d364", "yellow": "#e3b341", "blue": "#6ca4f8", "purple": "#db61a2", "cyan": "#2b7489", "white": "#ffffff", "brightBlack": "#4d4d4d", "brightRed": "#f78166", "brightGreen": "#56d364", "brightYellow": "#e3b341", "brightBlue": "#6ca4f8", "brightPurple": "#db61a2", "brightCyan": "#2b7489", "brightWhite": "#ffffff", "background": "#101216", "foreground": "#8b949e", "selectionBackground": "#ffffff", "cursor": "#101216", "cursorAccent": "#101216" }
Set TypingSpeed 0ms
Set Framerate 15
Set CursorBlink false

Hide
Type "set -e" Enter
Sleep 200ms
Type "export PATH=$PWD/.venv/bin:$PATH" Enter
Type "demo_root=$(mktemp -d /tmp/property-inventory-readme.XXXXXX)" Enter
Type "export PROPERTY_INVENTORY_ROOT=$demo_root/inventory" Enter
Type "export PROPERTY_INVENTORY_RUNTIME=$demo_root/runtime" Enter
Type "export PROPERTY_INVENTORY_MEDIA_ROOT=$demo_root/media" Enter
Type "export PROPERTY_INVENTORY_CATALOGUE_OUTPUT=$demo_root/notes/Inventory.md" Enter
Type "export PS1='$ '" Enter
Sleep 500ms
Type "property-inventory init >/dev/null" Enter
Sleep 5s
Type "property-inventory add-location --location-id loc-tool-drawer --name 'Tool drawer' --kind container >/dev/null" Enter
Sleep 5s
Type "property-inventory discover --actor Demo --source-ref 'Checked in person' --name 'T25 Torx bit' --category tool --checked-on 2026-08-09 --location-id loc-tool-drawer --new-model --new-unit --brand Wera --model T25 --quantity 1 --unit piece --condition working >/dev/null" Enter
Sleep 5s
Type "clear" Enter
Sleep 1s
Show
Sleep 500ms

Hide
Type "property-inventory "
Sleep 200ms
Show
Sleep 300ms
Hide
Type "search "
Sleep 200ms
Show
Sleep 300ms
Hide
Type "'T25' "
Sleep 200ms
Show
Sleep 300ms
Hide
Type "--summary" Enter
Sleep 3s
Show
Sleep 5s

Hide
Type "clear" Enter
Sleep 500ms
Show
Sleep 500ms
Hide
Type "case $demo_root in /tmp/property-inventory-readme.*) rm -rf $demo_root ;; esac" Enter
