Output docs/assets/property-inventory-demo.gif

Require jq

Set Shell "bash"
Set FontSize 18
Set LetterSpacing 0
Set LineHeight 1.1
Set Width 960
Set Height 560
Set Padding 28
Set Margin 16
Set MarginFill "#0d1117"
Set BorderRadius 12
Set WindowBar "Rings"
Set WindowBarSize 36
Set Theme "GitHub Dark"
Set TypingSpeed 12ms
Set Framerate 24

Hide
Type "pi=$(command -v property-inventory || printf '.venv/bin/property-inventory')" Enter
Type `property-inventory() { "$pi" "$@"; }` Enter
Type "demo_root=$(mktemp -d /tmp/property-inventory-readme.XXXXXX)" Enter
Type "export PROPERTY_INVENTORY_ROOT=$demo_root/inventory" Enter
Type "export PROPERTY_INVENTORY_RUNTIME=$demo_root/runtime" Enter
Type "export PROPERTY_INVENTORY_MEDIA_ROOT=$demo_root/media" Enter
Type "export PROPERTY_INVENTORY_CATALOGUE_OUTPUT=$demo_root/notes/Inventory.md" Enter
Type "export PS1='$ '" Enter
Type "property-inventory init >/dev/null" Enter
Type "property-inventory add-location --location-id loc-tool-drawer --name 'Tool drawer' --kind container >/dev/null" Enter
Type "property-inventory order --actor Demo --source-ref 'Example order' --name '1/4 inch hex bit set' --category tool --ordered-on 2026-08-09 --order-placed --new-model --brand Wera --model 'Bit-Check 30' --quantity 1 --unit set >/dev/null" Enter
Type "property-inventory receive --actor Demo --source-ref 'Checked on delivery' --item-id itm-1-4-inch-hex-bit-set --received-on 2026-08-09 --location-id loc-tool-drawer --quantity 1 --condition working --physical-check >/dev/null" Enter
Type "clear" Enter
Show

Type "# 1. SEARCH LIVE INVENTORY (jq only shortens output)" Enter
Type "property-inventory search 'hex bit' | jq -f docs/assets/demo-search.jq" Enter
Sleep 4s
Ctrl+L
Type "# 2. CHECK LEDGER HEALTH (jq only shortens output)" Enter
Type "property-inventory status | jq -f docs/assets/demo-status.jq" Enter
Sleep 4s

Hide
Type "case $demo_root in /tmp/property-inventory-readme.*) rm -rf $demo_root ;; esac" Enter
