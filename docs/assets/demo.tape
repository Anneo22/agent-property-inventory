Output docs/assets/property-inventory-demo.gif

Set Shell "bash"
Set FontSize 34
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
Set Theme { "black": "#000000", "red": "#f78166", "green": "#56d364", "yellow": "#e3b341", "blue": "#6ca4f8", "purple": "#db61a2", "cyan": "#39c5cf", "white": "#ffffff", "brightBlack": "#6e7681", "brightRed": "#f78166", "brightGreen": "#56d364", "brightYellow": "#e3b341", "brightBlue": "#6ca4f8", "brightPurple": "#db61a2", "brightCyan": "#56d4dd", "brightWhite": "#ffffff", "background": "#101216", "foreground": "#c9d1d9", "selectionBackground": "#ffffff", "cursor": "#101216", "cursorAccent": "#101216" }
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
Type "property-inventory add-location --location-id loc-cambridge-home --name 'Cambridge home' --kind site >/dev/null" Enter
Type "property-inventory add-location --location-id loc-study --parent-location-id loc-cambridge-home --name Study --kind room >/dev/null" Enter
Type "property-inventory add-location --location-id loc-drawer-unit --parent-location-id loc-study --name 'Drawer unit' --kind furniture >/dev/null" Enter
Type "property-inventory add-location --location-id loc-second-drawer --parent-location-id loc-drawer-unit --name 'Second drawer' --kind compartment >/dev/null" Enter
Type "property-inventory add-location --location-id loc-rear-section --parent-location-id loc-second-drawer --name 'Rear section' --kind compartment >/dev/null" Enter
Sleep 5s
Type "property-inventory discover --actor Demo --source-ref 'Checked in person' --name 'T25 Torx bit' --category tool --checked-on 2026-08-09 --location-id loc-study --container-id loc-rear-section --new-model --new-unit --brand Wera --model T25 --quantity 1 --unit piece --condition working >/dev/null" Enter
Sleep 5s
Type "clear" Enter
Sleep 1s
Show
Sleep 500ms

Hide
Type "# Ask: Do I own a T25 bit, and exactly where is it?" Enter
Sleep 800ms
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
Type "--summary | jq -C '.matches[0]'" Enter
Sleep 3s
Show
Sleep 4s

Hide
Type "clear" Enter
Sleep 500ms
Show
Sleep 500ms
Hide
Type "case $demo_root in /tmp/property-inventory-readme.*) rm -rf $demo_root ;; esac" Enter
