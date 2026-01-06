import openstep_plist


def openstepPlistFromPath(path):
    with open(path, "r") as fp:
        obj = openstep_plist.load(fp, use_numbers=True)
    return obj


def openstepPlistDumps(rawData):
    return (
        openstep_plist.dumps(
            rawData,
            unicode_escape=False,
            indent=0,
            single_line_tuples=True,
            escape_newlines=False,
            sort_keys=False,
            single_line_empty_objects=False,
            binary_spaces=False,
        )
        + "\n"
    )


def getSourceFromLayerName(sources, layerName):
    for source in sources:
        if source.layerName == layerName:
            return source
        if source.location == {}:
            defaultSource = source

    # NOTE: Theoretically it's possible to have a layer with no matching source.
    # In that case, fallback to default.
    return defaultSource


def splitLocation(location, glyphAxes):
    glyphAxisNames = {axis.name for axis in glyphAxes}

    fontLocation = {}
    glyphLocation = {}

    for axisName, axisValue in location.items():
        if axisName in glyphAxisNames:
            glyphLocation[axisName] = axisValue
        else:
            fontLocation[axisName] = axisValue

    return fontLocation, glyphLocation


LEAF = object()


def patternsToMatchTree(patterns):
    tree = {}
    for pattern in patterns:
        subtree = tree
        for item in pattern[:-1]:
            if item not in subtree:
                subtree[item] = {}
            subtree = subtree[item]
        subtree[pattern[-1]] = LEAF
    return tree


def convertMatchesToTuples(obj, matchTree, path=()):
    if isinstance(obj, dict):
        assert matchTree is not LEAF, path
        return {
            k: convertMatchesToTuples(
                v, matchTree.get(k, matchTree.get(None, {})), path + (k,)
            )
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        convertToTuple = False
        if matchTree is LEAF:
            convertToTuple = True
            matchTree = {}
        seq = [
            convertMatchesToTuples(item, matchTree.get(None, {}), path + (i,))
            for i, item in enumerate(obj)
        ]
        if convertToTuple:
            seq = tuple(seq)
        return seq
    else:
        return obj


patterns = [
    ["fontMaster", None, "guides", None, "pos"],
    ["glyphs", None, "color"],
    ["glyphs", None, "layers", None, "anchors", None, "pos"],
    ["glyphs", None, "layers", None, "background", "anchors", None, "pos"],
    ["glyphs", None, "layers", None, "annotations", None, "pos"],
    ["glyphs", None, "layers", None, "background", "shapes", None, "nodes", None],
    ["glyphs", None, "layers", None, "background", "shapes", None, "pos"],
    ["glyphs", None, "layers", None, "background", "shapes", None, "scale"],
    ["glyphs", None, "layers", None, "background", "shapes", None, "slant"],
    ["glyphs", None, "layers", None, "guides", None, "pos"],
    ["glyphs", None, "layers", None, "hints", None, "origin"],
    ["glyphs", None, "layers", None, "hints", None, "scale"],
    ["glyphs", None, "layers", None, "background", "hints", None, "origin"],
    ["glyphs", None, "layers", None, "background", "hints", None, "scale"],
    ["glyphs", None, "layers", None, "background", "hints", None, "place"],
    ["glyphs", None, "layers", None, "hints", None, "target"],
    ["glyphs", None, "layers", None, "shapes", None, "nodes", None],
    ["glyphs", None, "layers", None, "shapes", None, "pos"],
    ["glyphs", None, "layers", None, "shapes", None, "scale"],
]


matchTreeFont = patternsToMatchTree(patterns)
matchTreeGlyph = matchTreeFont["glyphs"][None]
