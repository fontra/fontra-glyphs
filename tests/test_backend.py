import asyncio
import os
import pathlib
import shutil
import uuid
from contextlib import aclosing
from copy import deepcopy

import glyphsLib
import openstep_plist
import pytest
from fontra.backends import getFileSystemBackend
from fontra.core.classes import (
    Anchor,
    Axes,
    FontInfo,
    GlyphAxis,
    GlyphSource,
    Guideline,
    Kerning,
    Layer,
    OpenTypeFeatures,
    StaticGlyph,
    VariableGlyph,
    structure,
)
from fontra.core.fonthandler import FontHandler
from fontra.filesystem.projectmanager import FileSystemProjectManager

from fontra_glyphs.backend import GlyphsBackendError

dataDir = pathlib.Path(__file__).resolve().parent / "data"

glyphs2Path = dataDir / "GlyphsUnitTestSans.glyphs"
glyphs3Path = dataDir / "GlyphsUnitTestSans3.glyphs"
glyphsPackagePath = dataDir / "GlyphsUnitTestSans3.glyphspackage"
expansionFontPath = dataDir / "FeatureExpansionTest.glyphs"
referenceFontPath = dataDir / "GlyphsUnitTestSans3.fontra"
rtlFontPath = dataDir / "right-to-left-kerning.glyphs"


def sourceNameMappingFromSources(fontSources):
    return {
        source.name: sourceIdentifier
        for sourceIdentifier, source in fontSources.items()
    }


@pytest.fixture(scope="module", params=[glyphs2Path, glyphs3Path, glyphsPackagePath])
def testFont(request):
    return getFileSystemBackend(request.param)


@pytest.fixture(scope="module")
def referenceFont(request):
    return getFileSystemBackend(referenceFontPath)


@pytest.fixture(params=[glyphs2Path, glyphs3Path, glyphsPackagePath])
def writableTestFont(tmpdir, request):
    srcPath = request.param
    dstPath = tmpdir / os.path.basename(srcPath)
    if os.path.isdir(srcPath):
        shutil.copytree(srcPath, dstPath)
    else:
        shutil.copy(srcPath, dstPath)
    return getFileSystemBackend(dstPath)


@pytest.fixture
def rtlTestFont():
    return getFileSystemBackend(rtlFontPath)


@pytest.fixture
def writableRTLTestFont(tmpdir):
    srcPath = rtlFontPath
    dstPath = tmpdir / srcPath.name
    shutil.copy(srcPath, dstPath)
    return getFileSystemBackend(dstPath)


expectedAxes = structure(
    {
        "axes": [
            {
                "defaultValue": 400,
                "hidden": False,
                "label": "Weight",
                "mapping": [
                    [100, 17],
                    [200, 30],
                    [300, 55],
                    [357, 75],
                    [400, 90],
                    [500, 133],
                    [700, 179],
                    [900, 220],
                ],
                "maxValue": 900,
                "minValue": 100,
                "name": "Weight",
                "tag": "wght",
            },
        ]
    },
    Axes,
)


@pytest.mark.asyncio
async def test_getAxes(testFont):
    axes = await testFont.getAxes()
    assert expectedAxes == axes


expectedGlyphMap = {
    "A": [65],
    "Adieresis": [196],
    "_part.shoulder": [],
    "_part.stem": [],
    "a": [97],
    "a.sc": [],
    "adieresis": [228],
    "dieresis": [168],
    "h": [104],
    "m": [109],
    "n": [110],
    "V": [86],
    "A-cy": [1040],
}


@pytest.mark.asyncio
async def test_getGlyphMap(testFont):
    glyphMap = await testFont.getGlyphMap()
    assert expectedGlyphMap == glyphMap


expectedFontInfo = FontInfo(
    familyName="Glyphs Unit Test Sans",
    versionMajor=1,
    versionMinor=0,
    copyright=None,
    trademark=None,
    description=None,
    sampleText=None,
    designer=None,
    designerURL=None,
    manufacturer=None,
    manufacturerURL=None,
    licenseDescription=None,
    licenseInfoURL=None,
    vendorID=None,
    customData={},
)


@pytest.mark.asyncio
async def test_getFontInfo(testFont):
    fontInfo = await testFont.getFontInfo()
    assert expectedFontInfo == fontInfo


@pytest.mark.asyncio
@pytest.mark.parametrize("glyphName", list(expectedGlyphMap))
async def test_getGlyph(testFont, referenceFont, glyphName):
    glyph = await testFont.getGlyph(glyphName)
    if glyphName == "A" and "com.glyphsapp.glyph-color" not in glyph.customData:
        # glyphsLib doesn't read the color attr from Glyphs-2 files,
        # so let's monkeypatch the data
        glyph.customData["com.glyphsapp.glyph-color"] = [120, 220, 20, 4]

    if (
        glyphName in ["h", "m", "n"]
        and "com.glyphsapp.glyph-color" not in glyph.customData
    ):
        # glyphsLib doesn't read the component alignment from Glyphs-2 files,
        # so let's monkeypatch the data
        for layerName in glyph.layers:
            for component in glyph.layers[layerName].glyph.components:
                if "com.glyphsapp.component.alignment" not in component.customData:
                    component.customData["com.glyphsapp.component.alignment"] = -1

    referenceGlyph = await referenceFont.getGlyph(glyphName)
    assert referenceGlyph == glyph


@pytest.mark.asyncio
@pytest.mark.parametrize("glyphName", list(expectedGlyphMap))
async def test_putGlyph(writableTestFont, glyphName):
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    # for testing change every coordinate by 10 units
    for layerName, layer in iter(glyph.layers.items()):
        layer.glyph.xAdvance = 500  # for testing change xAdvance
        for i, coordinate in enumerate(layer.glyph.path.coordinates):
            layer.glyph.path.coordinates[i] = coordinate + 10

    glyphCopy = deepcopy(glyph)
    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])
    assert glyphCopy == glyph  # putGlyph may not mutate

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph

    reopened = getFileSystemBackend(writableTestFont.path)
    reopenedGlyph = await reopened.getGlyph(glyphName)
    assert glyph == reopenedGlyph


@pytest.mark.asyncio
@pytest.mark.parametrize("gName", ["a", "A"])
async def test_duplicateGlyph(writableTestFont, gName):
    glyphName = f"{gName}.ss01"
    glyph = deepcopy(await writableTestFont.getGlyph(gName))
    glyph.name = glyphName
    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)

    # glyphsLib doesn't read the color attr from Glyphs-2 files,
    # so let's monkeypatch the data
    glyph.customData["com.glyphsapp.glyph-color"] = [120, 220, 20, 4]
    savedGlyph.customData["com.glyphsapp.glyph-color"] = [120, 220, 20, 4]

    assert glyph == savedGlyph

    if os.path.isdir(writableTestFont.path):
        # This is a glyphspackage:
        # check if the order.plist has been updated as well.
        packagePath = pathlib.Path(writableTestFont.path)
        orderPath = packagePath / "order.plist"
        with open(orderPath, "r", encoding="utf-8") as fp:
            glyphOrder = openstep_plist.load(fp, use_numbers=True)
            assert glyphName == glyphOrder[-1]


async def test_updateGlyphCodePoints(writableTestFont):
    # Use case: all uppercase font via double encodeding
    # for example: A -> A, a [0x0041, 0x0061]
    glyphName = "A"
    glyph = await writableTestFont.getGlyph(glyphName)
    codePoints = [0x0041, 0x0061]
    await writableTestFont.putGlyph(glyphName, glyph, codePoints)

    reopened = getFileSystemBackend(writableTestFont.path)
    reopenedGlyphMap = await reopened.getGlyphMap()
    assert reopenedGlyphMap["A"] == [0x0041, 0x0061]


async def test_updateSourceName(writableTestFont):
    glyphName = "a"
    glyph = await writableTestFont.getGlyph(glyphName)

    for i, source in enumerate(glyph.sources):
        source.name = f"source#{i}"

    await writableTestFont.putGlyph(glyphName, glyph, [ord("a")])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_createNewGlyph(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "a.ss02"
    glyph = VariableGlyph(name=glyphName)

    layerName = masterId = sourceNameMappingToIDs["Regular"]
    glyph.sources.append(
        GlyphSource(
            name="Default", location={}, locationBase=masterId, layerName=layerName
        )
    )
    glyph.layers[layerName] = Layer(glyph=StaticGlyph(xAdvance=333))

    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_createNewSmartGlyph(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "a.smart"
    glyphAxis = GlyphAxis(name="Height", minValue=0, maxValue=100, defaultValue=0)
    glyph = VariableGlyph(name=glyphName, axes=[glyphAxis])

    sourceInfo = [
        ("Light", {}, "Light"),
        ("Light-Height", {"Height": 100}, "Light"),
        ("Regular", {}, "Regular"),
        ("Regular-Height", {"Height": 100}, "Regular"),
        ("Bold", {}, "Bold"),
        ("Bold-Height", {"Height": 100}, "Bold"),
    ]

    # create a glyph with glyph axis
    for sourceName, location, associatedSourceName in sourceInfo:
        locationBase = sourceNameMappingToIDs[associatedSourceName]
        layerName = sourceNameMappingToIDs.get(sourceName) or str(uuid.uuid4()).upper()
        glyph.sources.append(
            GlyphSource(
                name=sourceName if location else "",
                location=location,
                locationBase=locationBase,
                layerName=layerName,
            )
        )
        glyph.layers[layerName] = Layer(glyph=StaticGlyph(xAdvance=100))

    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_extendSmartGlyphWithIntermediateLayerOnFontAxis(writableTestFont):
    # This should fail, because not yet implemented.
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(
            name="Intermediate Layer", location={"Weight": 99}, layerName=layerName
        )
    )
    glyph.layers[layerName] = Layer(glyph=StaticGlyph(xAdvance=100))

    with pytest.raises(
        NotImplementedError,
        match="Brace layers within smart glyphs are not yet implemented",
    ):
        await writableTestFont.putGlyph(glyphName, glyph, [])


async def test_extendSmartGlyphWithIntermediateLayerOnGlyphAxis(writableTestFont):
    # This should fail, because not yet implemented.
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(
            name="Intermediate Layer",
            location={"shoulderWidth": 50},
            layerName=layerName,
        )
    )
    glyph.layers[layerName] = Layer(glyph=StaticGlyph(xAdvance=100))

    with pytest.raises(
        NotImplementedError,
        match="Intermediate layers within smart glyphs are not yet implemented",
    ):
        await writableTestFont.putGlyph(glyphName, glyph, [])


async def test_smartGlyphAddGlyphAxisWithDefaultNotMinOrMax(writableTestFont):
    # This should fail, because not yet implemented.
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)
    glyphAxis = GlyphAxis(name="Height", minValue=0, maxValue=100, defaultValue=50)
    glyph.axes.append(glyphAxis)

    with pytest.raises(
        GlyphsBackendError,
        match="Glyph axis 'Height' defaultValue must be at MIN or MAX.",
    ):
        await writableTestFont.putGlyph(glyphName, glyph, [])


async def test_smartGlyphUpdateGlyphAxisWithDefaultNotMinOrMax(writableTestFont):
    # This should fail, because not yet implemented.
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)
    glyphAxis = glyph.axes[0]
    glyphAxis.defaultValue = 50

    with pytest.raises(
        GlyphsBackendError,
        match="defaultValue must be at MIN or MAX.",
    ):
        await writableTestFont.putGlyph(glyphName, glyph, [])


async def test_smartGlyphAddGlyphAxisWithDefaultAtMinOrMax(writableTestFont):
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)
    glyphAxis = GlyphAxis(name="Height", minValue=0, maxValue=100, defaultValue=100)
    glyph.axes.append(glyphAxis)

    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_smartGlyphRemoveGlyphAxis(writableTestFont):
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)
    del glyph.axes[0]

    # We expect we cannot roundtrip a glyph when removing a glyph axis,
    # because then some layers locations are not unique anymore.
    for i in [8, 5, 2]:
        del glyph.layers[glyph.sources[i].layerName]
        del glyph.sources[i]

    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_smartGlyphChangeGlyphAxisValue(writableTestFont):
    glyphName = "_part.shoulder"
    glyph = await writableTestFont.getGlyph(glyphName)

    glyph.axes[1].maxValue = 200
    # We expect we cannot roundtrip a glyph when changing a glyph axis min or
    # max value without changing the default, because in GlyphsApp there is
    # no defaultValue-concept. Therefore we need to change the defaultValue as well.
    glyph.axes[1].defaultValue = 200
    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_deleteLayer(writableTestFont):
    glyphName = "a"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)
    numGlyphLayers = len(glyph.layers)

    # delete intermediate layer
    sourceIndex = 1
    del glyph.layers[glyph.sources[sourceIndex].layerName + "^background"]
    del glyph.layers[glyph.sources[sourceIndex].layerName]
    del glyph.sources[sourceIndex]

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert len(savedGlyph.layers) < numGlyphLayers


async def test_addLayer(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "a"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(name="SemiBold", location={"Weight": 166}, layerName=layerName)
    )
    # Copy StaticGlyph from Bold:
    glyph.layers[layerName] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Bold"]].glyph)
    )

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_addBackgroundLayer(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "a"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    # add background layer:
    glyph.layers[sourceNameMappingToIDs.get("Regular") + "^background"] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs.get("Regular")].glyph)
    )

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_addBackgroundLayerToLayer(writableTestFont):
    # This is a nested behaviour.
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "A"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    # add layout layer:
    glyph.layers[sourceNameMappingToIDs.get("Regular") + "^Testing"] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs.get("Regular")].glyph),
        # Add explicit layerId for perfect round tripping
        customData={"com.glyphsapp.layer.layerId": str(uuid.uuid4()).upper()},
    )

    # add background to layout layer:
    glyph.layers[sourceNameMappingToIDs.get("Regular") + "^Testing/background"] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs.get("Regular")].glyph)
    )

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_addLayoutLayer(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "A"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    # add layout layer:
    glyph.layers[sourceNameMappingToIDs.get("Regular") + "^Layout Layer"] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Bold"]].glyph),
        # Add explicit layerId for perfect round tripping
        customData={"com.glyphsapp.layer.layerId": str(uuid.uuid4()).upper()},
    )

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_readBackgroundLayer(writableTestFont):
    glyphName = "a"
    glyph = await writableTestFont.getGlyph(glyphName)

    # every master layer of /a should have a background layer.
    for glyphSource in glyph.sources:
        assert f"{glyphSource.layerName}^background" in glyph.layers


async def test_addLayerWithoutSource(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "a"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    # Copy StaticGlyph from Bold:
    glyph.layers[layerName] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Bold"]].glyph)
    )

    with pytest.raises(
        GlyphsBackendError, match="Layer without glyph source is not supported"
    ):
        await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])


async def test_addLayerWithComponent(writableTestFont):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "n"  # n is made from components
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(name="SemiBold", location={"Weight": 166}, layerName=layerName)
    )
    # Copy StaticGlyph of Bold:
    glyph.layers[layerName] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Bold"]].glyph)
    )

    # add background layer
    glyph.layers[layerName + "^background"] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Bold"]].glyph)
    )

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)
    assert glyph == savedGlyph


async def test_addLayoutLayerToBraceLayer(writableTestFont):
    # This is a fundamental difference between Fontra and Glyphs. Therefore raise error.
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "n"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(name="SemiBold", location={"Weight": 166}, layerName=layerName)
    )

    # brace layer
    glyph.layers[layerName] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Light"]].glyph)
    )

    # secondary layer for brace layer
    glyph.layers[layerName + "^Layout Layer"] = Layer(
        glyph=deepcopy(glyph.layers[sourceNameMappingToIDs["Bold"]].glyph)
    )

    with pytest.raises(
        GlyphsBackendError,
        match="A brace layer can only have an additional source layer named 'background'",
    ):
        await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])


expectedSkewErrors = [
    # skewValue, expectedErrorMatch
    [20, "Does not support skewing of components"],
    [-0.001, "Does not support skewing of components"],
]


@pytest.mark.parametrize("skewValue,expectedErrorMatch", expectedSkewErrors)
async def test_skewComponent(writableTestFont, skewValue, expectedErrorMatch):
    fontSources = await writableTestFont.getSources()
    sourceNameMappingToIDs = sourceNameMappingFromSources(fontSources)
    glyphName = "Adieresis"  # Adieresis is made from components
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    glyph.layers[sourceNameMappingToIDs.get("Light")].glyph.components[
        0
    ].transformation.skewX = skewValue
    with pytest.raises(TypeError, match=expectedErrorMatch):
        await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])


async def test_addAnchor(writableTestFont):
    glyphName = "a"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(name="SemiBold", location={"Weight": 166}, layerName=layerName)
    )
    glyph.layers[layerName] = Layer(glyph=StaticGlyph(xAdvance=0))
    glyph.layers[layerName].glyph.anchors.append(Anchor(name="top", x=207, y=746))

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)

    assert (
        glyph.layers[layerName].glyph.anchors
        == savedGlyph.layers[layerName].glyph.anchors
    )


async def test_addGuideline(writableTestFont):
    glyphName = "a"
    glyphMap = await writableTestFont.getGlyphMap()
    glyph = await writableTestFont.getGlyph(glyphName)

    layerName = str(uuid.uuid4()).upper()
    glyph.sources.append(
        GlyphSource(name="SemiBold", location={"Weight": 166}, layerName=layerName)
    )
    glyph.layers[layerName] = Layer(glyph=StaticGlyph(xAdvance=0))
    glyph.layers[layerName].glyph.guidelines.append(Guideline(name="top", x=207, y=746))

    await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    savedGlyph = await writableTestFont.getGlyph(glyphName)

    assert (
        glyph.layers[layerName].glyph.guidelines
        == savedGlyph.layers[layerName].glyph.guidelines
    )


async def test_getKerning(testFont, referenceFont):
    assert await testFont.getKerning() == await referenceFont.getKerning()


def modifyKerningPair(kerning):
    kerning["kern"].values["@A"]["@J"][0] = -40
    return kerning


def deleteKerningPair(kerning):
    del kerning["kern"].values["@A"]["@J"]
    return kerning


def modifyKerningGroups(kerning):
    kerning["kern"].groupsSide1["A"].append("Adieresis")
    kerning["kern"].groupsSide2["A"].append("Adieresis")
    return kerning


def deleteAllKerning(kerning):
    return {}


def addUnknownSourceKerning(kerning):
    return {
        "kern": Kerning(
            groupsSide1={"A": ["A"]}, groupsSide2={}, sourceIdentifiers=["X"], values={}
        )
    }


putKerningTestData = [
    (modifyKerningPair, None),
    (deleteKerningPair, None),
    (modifyKerningGroups, None),
    (deleteAllKerning, None),
    (addUnknownSourceKerning, GlyphsBackendError),
]


@pytest.mark.parametrize("modifierFunction, expectedException", putKerningTestData)
async def test_putKerning(writableTestFont, modifierFunction, expectedException):
    kerning = await writableTestFont.getKerning()

    if writableTestFont.gsFont.format_version == 2:
        kerning.pop(
            "vkrn", None
        )  # glyphsLib does not support writing of vertical kerning

    kerning = modifierFunction(kerning)

    if expectedException:
        with pytest.raises(GlyphsBackendError):
            async with aclosing(writableTestFont):
                await writableTestFont.putKerning(kerning)
    else:
        async with aclosing(writableTestFont):
            await writableTestFont.putKerning(kerning)

        reopened = getFileSystemBackend(writableTestFont.path)
        reopenedKerning = await reopened.getKerning()
        assert reopenedKerning == kerning


async def test_putKerning_master_order(tmpdir):
    tmpdir = pathlib.Path(tmpdir)
    srcPath = pathlib.Path(glyphs3Path)
    dstPath = tmpdir / srcPath.name
    shutil.copy(srcPath, dstPath)

    testFont = getFileSystemBackend(dstPath)
    async with aclosing(testFont):
        await testFont.putKerning(await testFont.getKerning())

    assert srcPath.read_text() == dstPath.read_text()


async def test_getFeatures(testFont, referenceFont):
    assert await testFont.getFeatures() == await referenceFont.getFeatures()


async def test_getFeatures_with_expansion():
    expansionFontPath = dataDir / "FeatureExpansionTest.glyphs"
    testFont = getFileSystemBackend(expansionFontPath)
    features = await testFont.getFeatures()

    assert "WARNING" in features.text
    assert "@TOKEN_TESTING_CLASS = [A Adieresis A-cy];" in features.text
    assert "lookup testing_lookup {" in features.text

    glyphsSource = expansionFontPath.read_text(encoding="utf-8")

    assert "WARNING" not in glyphsSource
    assert "@TOKEN_TESTING_CLASS = [A Adieresis A-cy];" not in glyphsSource
    assert "lookup testing_lookup {" not in glyphsSource


putFeaturesTestData = [
    "# dummy feature data\n",
    """@c2sc_source = [ A
];

@c2sc_target = [ a.sc
];

# Prefix: Languagesystems
# Demo feature code for testing

languagesystem DFLT dflt; # Default, Default
languagesystem latn dflt; # Latin, Default

feature c2sc {
sub @c2sc_source by @c2sc_target;
} c2sc;
""",
    "syntax error",
]


@pytest.mark.parametrize("featureText", putFeaturesTestData)
async def test_putFeatures(writableTestFont, featureText):
    async with aclosing(writableTestFont):
        await writableTestFont.putFeatures(OpenTypeFeatures(text=featureText))

    reopened = getFileSystemBackend(writableTestFont.path)
    features = await reopened.getFeatures()
    assert features.text == featureText


async def test_locationBaseWrite(writableTestFont):
    glyphName = "q"  # Any glyph that doesn't exist yet

    fontSources = await writableTestFont.getSources()

    glyph = VariableGlyph(name=glyphName)

    for sourceIdentifier in fontSources.keys():
        glyph.sources.append(
            GlyphSource(
                name="", locationBase=sourceIdentifier, layerName=sourceIdentifier
            )
        )
        glyph.layers[sourceIdentifier] = Layer(glyph=StaticGlyph(xAdvance=333))

    await writableTestFont.putGlyph(glyphName, glyph, [])

    savedGlyph = await writableTestFont.getGlyph(glyphName)

    for (sourceIdentifier, fontSource), glyphSource in zip(
        fontSources.items(), savedGlyph.sources, strict=True
    ):
        assert glyphSource.name == ""
        glyphSource.location == {}

    assert glyph.layers == savedGlyph.layers


async def test_deleteGlyph(writableTestFont):
    glyphName = "A"

    async with aclosing(writableTestFont):
        await writableTestFont.deleteGlyph(glyphName)

    reopened = getFileSystemBackend(writableTestFont.path)
    glyphMap = await reopened.getGlyphMap()
    assert glyphName not in glyphMap

    glyph = await reopened.getGlyph(glyphName)
    assert glyph is None


async def test_deleteGlyph_addGlyph(writableTestFont):
    # This test (ab)uses the fact that the glyphMap order reveals the
    # glyph order in the .glyphs or .glyphspackage file.

    glyphName = "A"

    glyphMap = await writableTestFont.getGlyphMap()
    beforeKerning = await writableTestFont.getKerning()
    beforeGlyphOrder = list(glyphMap)
    glyph = await writableTestFont.getGlyph(glyphName)

    async with aclosing(writableTestFont):
        await writableTestFont.deleteGlyph(glyphName)
        await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    reopened = getFileSystemBackend(writableTestFont.path)
    glyphMap = await reopened.getGlyphMap()
    assert glyphName in glyphMap
    afterKerning = await reopened.getKerning()

    glyph = await reopened.getGlyph(glyphName)
    assert glyph is not None

    afterGlyphOrder = list(glyphMap)
    assert beforeGlyphOrder == afterGlyphOrder
    assert beforeKerning == afterKerning


async def test_writeFontData_glyphspackage_empty_glyphs_list(tmpdir):
    tmpdir = pathlib.Path(tmpdir)
    srcPath = pathlib.Path(glyphsPackagePath)
    dstPath = tmpdir / srcPath.name
    fontInfoPath = dstPath / "fontinfo.plist"
    shutil.copytree(srcPath, dstPath)
    fontInfoBefore = fontInfoPath.read_text()

    testFont = getFileSystemBackend(dstPath)
    async with aclosing(testFont):
        await testFont.putKerning(await testFont.getKerning())

    fontInfoAfter = fontInfoPath.read_text()

    assert fontInfoAfter == fontInfoBefore


@pytest.mark.parametrize(
    "glyphName, expectedUsedBy",
    [
        ("A", ["A-cy", "Adieresis"]),
        ("a", ["adieresis"]),
        ("_part.shoulder", ["h", "m", "n"]),
        ("dieresis", ["Adieresis", "adieresis"]),
        ("V", []),
        ("V.undefined", []),
    ],
)
async def test_findGlyphsThatUseGlyph(testFont, glyphName, expectedUsedBy):
    usedBy = await testFont.findGlyphsThatUseGlyph(glyphName)
    assert usedBy == expectedUsedBy


async def setupFontHandler(backend):
    fh = FontHandler(
        backend=backend,
        projectIdentifier="test",
        metaInfoProvider=FileSystemProjectManager(),
    )
    await fh.startTasks()
    return fh


@pytest.mark.parametrize("changeUnicodes", [False, True])
async def test_externalChanges_putGlyph(writableTestFont, changeUnicodes):
    listenerFont = getFileSystemBackend(writableTestFont.path)
    listenerHandler = await setupFontHandler(listenerFont)

    glyphName = "A"

    async with aclosing(listenerHandler):
        listenerGlyphMap = await listenerHandler.getGlyphMap()  # load in cache
        listenerGlyph = await listenerHandler.getGlyph(glyphName)  # load in cache

        glyphMap = await writableTestFont.getGlyphMap()
        if changeUnicodes:
            glyphMap[glyphName] = glyphMap[glyphName] + [0x1234]

        glyph = await writableTestFont.getGlyph(glyphName)
        layerGlyph = glyph.layers[glyph.sources[0].layerName].glyph
        layerGlyph.path.coordinates[0] = 999

        await writableTestFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

        await asyncio.sleep(0.15)  # give the file watcher a moment to catch up

        listenerGlyph = await listenerHandler.getGlyph(glyphName)
        assert glyph == listenerGlyph

        listenerGlyphMap = await listenerHandler.getGlyphMap()
        assert glyphMap == listenerGlyphMap


async def test_externalChanges_addGlyph(writableTestFont):
    listenerFont = getFileSystemBackend(writableTestFont.path)
    listenerHandler = await setupFontHandler(listenerFont)

    sourceGlyphName = "h"
    destGlyphName = "h.alt"

    async with aclosing(listenerHandler):
        glyph = await writableTestFont.getGlyph(sourceGlyphName)
        glyph.name = destGlyphName

        await writableTestFont.putGlyph(destGlyphName, glyph, [])

        await asyncio.sleep(0.15)  # give the file watcher a moment to catch up

        listenerGlyph = await listenerHandler.getGlyph(destGlyphName)
        assert glyph == listenerGlyph

        listenerGlyphMap = await listenerHandler.getGlyphMap()
        assert destGlyphName in listenerGlyphMap


async def test_externalChanges_deleteGlyph(writableTestFont):
    listenerFont = getFileSystemBackend(writableTestFont.path)
    listenerHandler = await setupFontHandler(listenerFont)

    glyphName = "h"

    async with aclosing(listenerHandler):
        listenerGlyph = await listenerHandler.getGlyph(glyphName)  # load in cache

        await writableTestFont.deleteGlyph(glyphName)

        await asyncio.sleep(0.15)  # give the file watcher a moment to catch up

        listenerGlyph = await listenerHandler.getGlyph(glyphName)
        assert listenerGlyph is None

        listenerGlyphMap = await listenerHandler.getGlyphMap()
        assert glyphName not in listenerGlyphMap


async def test_externalChanges_putKerning(writableTestFont):
    listenerFont = getFileSystemBackend(writableTestFont.path)
    listenerHandler = await setupFontHandler(listenerFont)

    async with aclosing(listenerHandler):
        listenerKerning = await listenerHandler.getKerning()  # load in cache
        del listenerKerning["vkrn"]  # skip vkrn, doesn't work for Glyphs 2

        kerning = await writableTestFont.getKerning()
        del kerning["vkrn"]  # skip vkrn, doesn't work for Glyphs 2
        kerning["kern"].values["@A"]["@J"][1] = 999

        await writableTestFont.putKerning(kerning)

        await asyncio.sleep(0.15)  # give the file watcher a moment to catch up

        listenerKerning = await listenerHandler.getKerning()
        assert kerning == listenerKerning


async def test_externalChanges_putFeatures(writableTestFont):
    listenerFont = getFileSystemBackend(writableTestFont.path)
    listenerHandler = await setupFontHandler(listenerFont)

    async with aclosing(listenerHandler):
        listenerFeatures = await listenerHandler.getFeatures()  # load in cache

        features = await writableTestFont.getFeatures()
        features.text += "\nfeature test {\nsub A by a;\n} test;\n"

        await writableTestFont.putFeatures(features)

        await asyncio.sleep(0.15)  # give the file watcher a moment to catch up

        listenerFeatures = await listenerHandler.getFeatures()
        assert features == listenerFeatures


async def test_deleteUnknownGlyph(writableTestFont):
    glyphName = "A.doesnotexist"
    glyphMap = await writableTestFont.getGlyphMap()
    assert glyphName not in glyphMap
    # Should *not* raise an exception
    await writableTestFont.deleteGlyph(glyphName)


async def test_glyphClassifications(rtlTestFont):
    expectedLTRGlyphs = {
        "A",
        "B",
        "C",
        "O",
        "T",
        "V",
        "W",
        "a",
        "b",
        "c",
        "d",
        "e",
        "four",
        "four.denominator",
        "four.numerator",
        "four.tlf",
        "fourinferior",
        "foursuperior",
        "o",
        "one",
        "one.denominator",
        "one.numerator",
        "one.tlf",
        "oneinferior",
        "onesuperior",
        "ordfeminine",
        "ordmasculine",
        "three",
        "three.denominator",
        "three.numerator",
        "three.tlf",
        "threeinferior",
        "threesuperior",
        "two",
        "two.denominator",
        "two.numerator",
        "two.tlf",
        "twoinferior",
        "twosuperior",
        "v",
        "w",
        "z",
        "zero",
        "zero.denominator",
        "zero.numerator",
        "zero.tlf",
        "zeroinferior",
        "zerosuperior",
    }
    expectedRTLGlyphs = {
        "alef",
        "alef.fina",
        "alef.isol",
        "alefHamzeAbove",
        "alefHamzeAbove.fina",
        "alefHamzeAbove.isol",
        "alefHamzeBelow",
        "alefHamzeBelow.fina",
        "alefHamzeBelow.isol",
        "alefMaghsureh",
        "alefMaghsureh.alt",
        "alefMaghsureh.fina",
        "alefMaghsureh.fina.alt",
        "alefMaghsureh.isol",
        "alefMaghsureh.isol.alt",
        "allah",
        "beh",
        "beh.fina",
        "beh.init",
        "beh.isol",
        "beh.medi",
        "dal",
        "dal.fina",
        "dal.isol",
        "eyn",
        "eyn.alt",
        "eyn.fina",
        "eyn.fina.alt",
        "eyn.fina.jump.alt",
        "eyn.init",
        "eyn.init.alt",
        "eyn.isol",
        "eyn.isol.alt",
        "eyn.medi",
        "feh",
        "feh.fina",
        "feh.init",
        "feh.isol",
        "feh.medi",
        "ghaf",
        "ghaf.fina",
        "ghaf.init",
        "ghaf.isol",
        "ghaf.medi",
        "hah",
        "hah.alt",
        "hah.fina",
        "hah.fina.alt",
        "hah.init",
        "hah.init.alt",
        "hah.isol",
        "hah.isol.alt",
        "hah.medi",
        "hah.medi.alt",
        "heh",
        "heh.fina",
        "heh.init",
        "heh.isol",
        "heh.medi",
        "jim",
        "jim.alt",
        "jim.fina",
        "jim.fina.alt",
        "jim.init",
        "jim.init.alt",
        "jim.isol",
        "jim.isol.alt",
        "jim.medi",
        "jim.medi.alt",
        "kafArabic",
        "kafArabic.fina",
        "kafArabic.init",
        "kafArabic.isol",
        "kafArabic.medi",
        "kheh",
        "kheh.fina",
        "kheh.init",
        "kheh.isol",
        "kheh.medi",
        "lam",
        "lam.fina",
        "lam.init",
        "lam.init_alef.fina",
        "lam.init_alefHamzeAbove.fina",
        "lam.init_alefHamzeBelow.fina",
        "lam.isol",
        "lam.medi",
        "lam.medi_alef.fina",
        "lam.medi_alefHamzeAbove.fina",
        "lam.medi_alefHamzeBelow.fina",
        "mim",
        "mim.fina",
        "mim.init",
        "mim.isol",
        "mim.medi",
        "noon",
        "noon.fina",
        "noon.init",
        "noon.isol",
        "noon.medi",
        "reh",
        "reh.fina",
        "reh.isol",
        "sad",
        "sad.fina",
        "sad.init",
        "sad.isol",
        "sad.medi",
        "shin",
        "shin.fina",
        "shin.init",
        "shin.isol",
        "shin.medi",
        "sin",
        "sin.fina",
        "sin.init",
        "sin.isol",
        "sin.medi",
        "ta",
        "ta.fina",
        "ta.init",
        "ta.isol",
        "ta.medi",
        "teh",
        "teh.fina",
        "teh.init",
        "teh.isol",
        "teh.medi",
        "tehArabic",
        "tehArabic.fina",
        "tehArabic.isol",
        "unencodedrtlglyph",
        "vav",
        "vav.fina",
        "vav.isol",
        "yehArabic",
        "yehArabic.fina",
        "yehArabic.init",
        "yehArabic.isol",
        "yehArabic.medi",
        "zad",
        "zad.fina",
        "zad.init",
        "zad.isol",
        "zad.medi",
        "zal",
        "zal.fina",
        "zal.isol",
        "zeh",
        "zeh.fina",
        "zeh.isol",
    }

    ltrGlyphs, rtlGlyphs = await rtlTestFont._getGlyphClassifications()
    assert (ltrGlyphs, rtlGlyphs) == (expectedLTRGlyphs, expectedRTLGlyphs)


expectedKerning = {
    "kern": Kerning(
        groupsSide1={
            "A": ["A"],
            "C": ["C"],
            "O": ["O"],
            "T": ["T"],
            "W": ["W"],
            "a": ["a"],
            "c": ["c"],
            "d": ["d"],
            "dal.isol": ["dal.isol", "zal.isol"],
            "dotlessBeh": ["beh.isol", "teh.isol"],
            "e": ["e"],
            "eyn.init": ["eyn.init"],
            "eyn.init.alt": ["eyn.init.alt"],
            "feh.isol": ["feh.init", "feh.isol"],
            "heh.init": ["heh.init"],
            "heh.isol": ["heh.isol"],
            "hyphen": ["hyphen"],
            "hyphen.case": ["hyphen.case"],
            "jim.init": ["hah.init", "jim.init", "kheh.init"],
            "jim.init.alt": ["hah.init.alt", "jim.init.alt"],
            "jim.isol": ["hah.isol", "jim.isol", "kheh.isol"],
            "jim.isol.alt": ["hah.isol.alt", "jim.isol.alt"],
            "kafArabic.init": ["kafArabic.init"],
            "mim.isol": ["mim.init", "mim.isol"],
            "noonGhuna.isol": ["noon.isol"],
            "o": ["b", "o"],
            "period": ["period"],
            "sad.isol": ["sad.init", "sad.isol", "zad.init", "zad.isol"],
            "sin.isol": ["shin.init", "shin.isol", "sin.init", "sin.isol"],
            "ta.isol": ["ta.init", "ta.isol"],
            "theh.init": ["teh.init"],
            "w": ["w"],
            "yeh.isol": ["alefMaghsureh.isol", "yehArabic.isol"],
            "yeh.isol.alt": ["alefMaghsureh.isol.alt", "yeh.isol.alt"],
            "z": ["z"],
        },
        groupsSide2={
            "A": ["A"],
            "O": ["C", "O"],
            "T": ["T"],
            "W": ["W"],
            "alefHamzeAbove.fina": ["alefHamzeAbove.fina"],
            "dotlessBeh": ["beh.fina", "beh.isol", "teh.fina", "teh.isol"],
            "eyn.fina": ["eyn.fina"],
            "eyn.fina.alt": ["eyn.fina.alt", "eyn.fina.jump.alt"],
            "eyn.isol": ["eyn.isol"],
            "eyn.isol.alt": ["eyn.isol.alt"],
            "feh.isol": ["feh.fina", "feh.isol"],
            "h": ["b"],
            "heh.fina": ["heh.fina", "tehArabic.fina"],
            "heh.isol": ["heh.isol"],
            "hyphen": ["hyphen"],
            "hyphen.case": ["hyphen.case"],
            "jim.isol": [
                "hah.fina",
                "hah.isol",
                "jim.fina",
                "jim.isol",
                "kheh.fina",
                "kheh.isol",
            ],
            "jim.isol.alt": [
                "hah.fina.alt",
                "hah.isol.alt",
                "jim.fina.alt",
                "jim.isol.alt",
            ],
            "kafArabic.isol": ["kafArabic.fina", "kafArabic.isol"],
            "lam.init_alef.fina": ["lam.init_alef.fina", "lam.medi_alef.fina"],
            "lam.init_alefHamzeAbove.fina": [
                "lam.init_alefHamzeAbove.fina",
                "lam.medi_alefHamzeAbove.fina",
            ],
            "lam.init_alefHamzeBelow.fina": [
                "lam.init_alefHamzeBelow.fina",
                "lam.medi_alefHamzeBelow.fina",
            ],
            "o": ["a", "c", "d", "e", "o"],
            "period": ["period"],
            "reh.isol": ["reh.fina", "reh.isol"],
            "w": ["w"],
            "yeh.isol": [
                "alefMaghsureh.fina",
                "alefMaghsureh.isol",
                "yehArabic.fina",
                "yehArabic.isol",
            ],
            "yeh.isol.alt": [
                "alefMaghsureh.fina.alt",
                "alefMaghsureh.isol.alt",
                "yeh.fina.alt",
                "yeh.isol.alt",
            ],
            "z": ["z"],
            "zeh.isol": ["zeh.fina", "zeh.isol"],
        },
        sourceIdentifiers=["m001"],
        values={
            "B": {"@period": [-25]},
            "V": {
                "@A": [-53],
                "@hyphen": [-59],
                "@hyphen.case": [-24],
                "@o": [-47],
                "@period": [-79],
            },
            "@A": {
                "V": [-15],
                "@O": [-36],
                "@T": [-96],
                "@W": [-13],
                "@hyphen": [-39],
                "@hyphen.case": [-55],
                "@w": [-31],
                "v": [-41],
            },
            "@C": {"@hyphen.case": [-65], "@period": [-35]},
            "@O": {"V": [-32], "@A": [-13], "@T": [-54], "@W": [-11], "@period": [-73]},
            "@T": {
                "@A": [-76],
                "@O": [-33],
                "@hyphen": [-59],
                "@hyphen.case": [-63],
                "@o": [-102],
                "@period": [-94],
                "@w": [-80],
                "@z": [-83],
                "v": [-98],
            },
            "@W": {
                "@A": [-23],
                "@hyphen": [-62],
                "@hyphen.case": [-27],
                "@o": [-53],
                "@period": [-71],
            },
            "@a": {
                "V": [-24],
                "@O": [-19],
                "@T": [-78],
                "@W": [-24],
                "@w": [-27],
                "v": [-12],
            },
            "@c": {
                "V": [-34],
                "@T": [-84],
                "@W": [-42],
                "@w": [-36],
                "@z": [-35],
                "v": [-31],
            },
            "@d": {"@w": [-12], "v": [-25]},
            "@e": {"V": [-39], "@T": [-85], "@W": [-33], "v": [-18]},
            "@hyphen": {"V": [-42], "@A": [-28], "@T": [-53], "@W": [-52]},
            "@hyphen.case": {"V": [-29], "@A": [-40], "@T": [-61], "@W": [-50]},
            "@o": {"V": [-52], "@T": [-107], "@W": [-59], "@w": [-20], "v": [-53]},
            "@period": {
                "V": [-76],
                "one": [-76],
                "@O": [-71],
                "@T": [-94],
                "@W": [-70],
                "@period": [-58],
                "@w": [-65],
                "v": [-80],
            },
            "@w": {
                "@A": [-32],
                "@T": [-69],
                "@h": [-18],
                "@o": [-31],
                "@period": [-47],
            },
            "@z": {"@T": [-97], "@h": [-18], "@o": [-22]},
            "two": {"four": [-34]},
            "v": {"@A": [-37], "@T": [-86], "@h": [-21], "@o": [-52], "@period": [-79]},
            "@jim.isol.alt": {
                "@alefHamzeAbove.fina": [-12],
                "@eyn.fina.alt": [-128],
                "@heh.fina": [-28],
                "@heh.isol": [-21],
                "@kafArabic.isol": [-27],
                "@lam.init_alef.fina": [-94],
                "@lam.init_alefHamzeAbove.fina": [-86],
                "tehArabic.isol": [-17],
            },
            "@yeh.isol": {
                "@alefHamzeAbove.fina": [-35],
                "@dotlessBeh": [-79],
                "@eyn.fina.alt": [-228],
                "@feh.isol": [-89],
                "@heh.fina": [-208],
                "@heh.isol": [-142],
                "@kafArabic.isol": [-155],
                "@lam.init_alef.fina": [-74],
                "@lam.init_alefHamzeAbove.fina": [-78],
                "@yeh.isol": [-47],
                "tehArabic.isol": [-156],
            },
            "lam.init": {
                "@eyn.fina": [-13],
                "@eyn.isol": [-18],
                "@eyn.isol.alt": [-35],
                "@reh.isol": [-36],
                "@yeh.isol.alt": [-174],
            },
            "noon.init": {
                "@eyn.fina": [-23],
                "@eyn.isol": [-10],
                "@eyn.isol.alt": [-13],
                "@reh.isol": [-33],
                "@yeh.isol.alt": [-137],
            },
            "@heh.isol": {
                "@eyn.fina": [-28],
                "@eyn.isol": [-26],
                "@eyn.isol.alt": [-21],
                "@jim.isol.alt": [-20],
                "@lam.init_alef.fina": [-17],
                "@lam.init_alefHamzeAbove.fina": [-14],
                "@lam.init_alefHamzeBelow.fina": [-30],
                "@reh.isol": [-20],
                "@yeh.isol.alt": [-226],
            },
            "@mim.isol": {
                "@eyn.fina": [-27],
                "@eyn.isol": [-36],
                "@eyn.isol.alt": [-18],
                "@jim.isol": [-13],
                "@jim.isol.alt": [-7],
                "@lam.init_alef.fina": [-11],
                "@lam.init_alefHamzeAbove.fina": [-22],
                "@lam.init_alefHamzeBelow.fina": [-8],
                "@reh.isol": [-26],
                "@yeh.isol.alt": [-229],
            },
            "@sad.isol": {
                "@eyn.fina": [-22],
                "@reh.isol": [-34],
                "@yeh.isol.alt": [-232],
            },
            "@theh.init": {
                "@eyn.fina": [-29],
                "@eyn.isol": [-32],
                "@eyn.isol.alt": [-31],
                "@reh.isol": [-22],
                "@yeh.isol.alt": [-66],
            },
            "tehArabic.isol": {
                "@eyn.fina": [-37],
                "@eyn.isol": [-24],
                "@eyn.isol.alt": [-29],
                "@jim.isol.alt": [-48],
                "@lam.init_alef.fina": [-26],
                "@lam.init_alefHamzeBelow.fina": [-10],
                "@reh.isol": [-30],
                "@yeh.isol.alt": [-214],
            },
            "@eyn.init": {
                "@eyn.fina.alt": [-26],
                "@reh.isol": [-31],
                "@yeh.isol.alt": [-246],
                "@zeh.isol": [-31],
            },
            "@jim.init": {
                "@eyn.fina.alt": [-101],
                "@reh.isol": [-44],
                "@yeh.isol.alt": [-236],
                "@zeh.isol": [-39],
            },
            "@jim.init.alt": {
                "@eyn.fina.alt": [-75],
                "@lam.init_alef.fina": [-33],
                "@lam.init_alefHamzeAbove.fina": [-36],
                "@reh.isol": [-65],
                "@yeh.isol.alt": [-191],
                "@zeh.isol": [-58],
            },
            "@jim.isol": {"@eyn.fina.alt": [-96]},
            "@yeh.isol.alt": {
                "@eyn.fina.alt": [-63],
                "@reh.isol": [-42],
                "@yeh.isol.alt": [-156],
                "@zeh.isol": [-32],
            },
            "reh.isol": {
                "@eyn.fina.alt": [-103],
                "@heh.fina": [-29],
                "@kafArabic.isol": [-11],
            },
            "zeh.isol": {
                "@eyn.fina.alt": [-19],
                "@heh.fina": [-14],
                "@kafArabic.isol": [-20],
            },
            "unencodedrtlglyph": {"@period": [-49]},
            "beh.init": {"@reh.isol": [-27]},
            "ghaf.init": {"@reh.isol": [-37], "@yeh.isol.alt": [-222]},
            "lam.init_alef.fina": {
                "@reh.isol": [-34],
                "@yeh.isol.alt": [-237],
                "@zeh.isol": [-29],
            },
            "lam.init_alefHamzeAbove.fina": {
                "@reh.isol": [-37],
                "@yeh.isol.alt": [-241],
                "@zeh.isol": [-39],
            },
            "lam.init_alefHamzeBelow.fina": {
                "@reh.isol": [-38],
                "@yeh.isol.alt": [-244],
                "@zeh.isol": [-29],
            },
            "@dal.isol": {
                "@reh.isol": [-21],
                "@yeh.isol.alt": [-227],
                "@zeh.isol": [-29],
            },
            "@dotlessBeh": {"@reh.isol": [-28], "@yeh.isol.alt": [-223]},
            "@eyn.init.alt": {
                "@reh.isol": [-46],
                "@yeh.isol.alt": [-253],
                "@zeh.isol": [-36],
            },
            "@feh.isol": {"@reh.isol": [-36], "@yeh.isol.alt": [-234]},
            "@heh.init": {"@reh.isol": [-35], "@yeh.isol.alt": [-211]},
            "@kafArabic.init": {"@reh.isol": [-12], "@yeh.isol.alt": [-238]},
            "@noonGhuna.isol": {"@reh.isol": [-21], "@yeh.isol.alt": [-97]},
            "@sin.isol": {"@reh.isol": [-51], "@yeh.isol.alt": [-221]},
            "@ta.isol": {"@reh.isol": [-21], "@yeh.isol.alt": [-218]},
            "vav.isol": {"@reh.isol": [-31]},
        },
    )
}


async def test_read_rtl_kerning(rtlTestFont):
    kerning = await rtlTestFont.getKerning()
    sortKernGroups(kerning)

    assert kerning == expectedKerning


async def test_write_rtl_kerning(writableRTLTestFont):
    glyphsPath = writableRTLTestFont.path

    gsFont = glyphsLib.GSFont(glyphsPath)
    kernSides = extractKernSides(gsFont)

    allFontraKerning = await writableRTLTestFont.getKerning()

    await writableRTLTestFont.putKerning(allFontraKerning)

    reopened = getFileSystemBackend(glyphsPath)
    reopenedKerning = await reopened.getKerning()
    assert reopenedKerning == allFontraKerning

    reopenedGSFont = glyphsLib.GSFont(glyphsPath)
    reopenedKernSides = extractKernSides(reopenedGSFont)

    assert unorderKerning(gsFont.kerning) == unorderKerning(reopenedGSFont.kerning)
    assert unorderKerning(gsFont.kerningRTL) == unorderKerning(
        reopenedGSFont.kerningRTL
    )
    assert kernSides == reopenedKernSides


async def test_modify_kerning(writableRTLTestFont):
    glyphsPath = writableRTLTestFont.path

    gsFont = glyphsLib.GSFont(glyphsPath)
    kernSides = extractKernSides(gsFont)
    gsKerningLTR = gsFont.kerning
    gsKerningRTL = gsFont.kerningRTL

    kerning = await writableRTLTestFont.getKerning()

    assert "B" not in kerning["kern"].groupsSide1
    kerning["kern"].groupsSide1["B"] = ["B"]
    kerning["kern"].groupsSide1["commaArabic"] = ["commaArabic"]
    kerning["kern"].values["@B"] = {"C": [-50]}
    kerning["kern"].values["@commaArabic"] = {"alef": [-75]}

    kernSides["B"] = (None, "B")
    kernSides["commaArabic"] = (None, "commaArabic")
    gsKerningLTR["m001"]["@MMK_L_B"] = {"C": -50}
    gsKerningRTL["m001"]["alef"] = {"@MMK_L_commaArabic": -75}

    await writableRTLTestFont.putKerning(kerning)

    reopened = getFileSystemBackend(glyphsPath)
    reopenedKerning = await reopened.getKerning()

    assert kerning == reopenedKerning

    reopenedGSFont = glyphsLib.GSFont(glyphsPath)
    reopenedKernSides = extractKernSides(reopenedGSFont)

    assert unorderKerning(gsKerningLTR) == unorderKerning(reopenedGSFont.kerning)
    assert unorderKerning(gsKerningRTL) == unorderKerning(reopenedGSFont.kerningRTL)
    assert kernSides == reopenedKernSides


def extractKernSides(gsFont):
    return {
        glyph.name: (glyph.leftKerningGroup, glyph.rightKerningGroup)
        for glyph in gsFont.glyphs
        if glyph.leftKerningGroup or glyph.rightKerningGroup
    }


def sortKernGroups(kerning):
    for kernTable in kerning.values():
        sortGroups(kernTable.groupsSide1)
        sortGroups(kernTable.groupsSide2)


def sortGroups(groups):
    for k, v in groups.items():
        v.sort()


def unorderKerning(kerning):
    return {
        masterID: {left: dict(leftDict) for left, leftDict in masterKerning.items()}
        for masterID, masterKerning in kerning.items()
    }
