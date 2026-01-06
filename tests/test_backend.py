import asyncio
import os
import pathlib
import shutil
import uuid
from contextlib import aclosing
from copy import deepcopy

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
referenceFontPath = dataDir / "GlyphsUnitTestSans3.fontra"


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
            groupsSide1={}, groupsSide2={}, sourceIdentifiers=["X"], values={}
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


async def test_putFeatures(writableTestFont):
    featureText = "# dummy feature data\n"
    async with aclosing(writableTestFont):
        await writableTestFont.putFeatures(OpenTypeFeatures(text=featureText))

    reopened = getFileSystemBackend(writableTestFont.path)
    features = await reopened.getFeatures()
    assert features.text == featureText


async def test_putFeatures_with_feature_text(writableTestFont):
    featureText = """@c2sc_source = [ A
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
"""
    async with aclosing(writableTestFont):
        await writableTestFont.putFeatures(OpenTypeFeatures(text=featureText))

    reopened = getFileSystemBackend(writableTestFont.path)
    features = await reopened.getFeatures()
    assert features.text == featureText


async def test_putFeatures_with_feature_text_failing(writableTestFont):
    featureText = """@case_source = [ at
];

@case_target = [ at.sc
];

# Prefix: Languagesystems
# Demo feature code for testing

languagesystem DFLT dflt; # Default, Default
languagesystem latn dflt; # Latin, Default

feature case {
sub @case_source by @case_target;
} case;
"""
    with pytest.raises(
        GlyphsBackendError,
        match="The following glyph names are referenced but are missing from the glyph set:",
    ):
        await writableTestFont.putFeatures(OpenTypeFeatures(text=featureText))


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
