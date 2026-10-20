The Share machine panel's heading now shows the person-with-plus icon beside
"Share machine:", matching the titlebar button the panel opens from. It asked
for a glyph named `share`, which the icon set does not carry, so the heading
reserved the space and drew nothing into it.

`Icon16` and `Icon12` now take a name typed to the glyphs that actually exist,
so a name with no artwork is a build error rather than an invisible gap.
