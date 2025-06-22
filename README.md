# fontra-glyphs

Fontra file system backend for the Glyphs app file format.

It supports the following features:

- Brace layers
- Smart components (for now restricted to interpolation: axis values need to be within the minimum and maximum values)

### Writing is currently limited to ...

#### Glyph Layer

- Contour (Paths, Nodes) ✅
- (Smart) Components ✅
- Anchors ✅
- Guidelines ✅

#### Features

- featurePrefixes ✅
- features ✅
- classes ✅
