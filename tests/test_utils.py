import os
import pathlib
import shutil

import openstep_plist
import pytest
from fontra.backends import getFileSystemBackend
from fontra.core.varutils import makeDenseLocation
from glyphsLib.classes import GSAxis, GSFont, GSFontMaster, GSGlyph, GSLayer
from test_backend import expectedGlyphMap

from fontra_glyphs.utils import (
    convertMatchesToTuples,
    getSourceFromLayerName,
    matchTreeFont,
    openstepPlistDumps,
    splitLocation,
)

dataDir = pathlib.Path(__file__).resolve().parent / "data"

glyphs2Path = dataDir / "GlyphsUnitTestSans.glyphs"
glyphs3Path = dataDir / "GlyphsUnitTestSans3.glyphs"
glyphsPackagePath = dataDir / "GlyphsUnitTestSans3.glyphspackage"


@pytest.fixture(scope="module", params=[glyphs2Path, glyphs3Path, glyphsPackagePath])
def testFont(request):
    return getFileSystemBackend(request.param)


def createGSFontMaster(axes=[100, 100], id="DUMMY-MASTER-ID"):
    master = GSFontMaster()
    master.axes = axes
    master.id = id
    return master


def createGSGlyph(name="GlyphName", unicodes=[], layers=[]):
    glyph = GSGlyph()
    glyph.name = name
    glyph.unicodes = unicodes
    glyph.layers = layers
    return glyph


@pytest.fixture(scope="module")
def testGSFontWW():
    gsFont = GSFont()
    gsFont.format_version = 3
    gsFont.axes = [
        GSAxis(name="Optical Size", tag="opsz"),
        GSAxis(name="Weight", tag="wght"),
        GSAxis(name="Width", tag="wdth"),
    ]
    gsFont.masters = [
        createGSFontMaster(axes=[12, 50, 100], id="MasterID-TextCondLight"),
        createGSFontMaster(axes=[12, 50, 400], id="MasterID-TextCondRegular"),
        createGSFontMaster(axes=[12, 50, 900], id="MasterID-TextCondBold"),
        createGSFontMaster(axes=[12, 200, 100], id="MasterID-TextWideLight"),
        createGSFontMaster(axes=[12, 200, 400], id="MasterID-TextWideRegular"),
        createGSFontMaster(axes=[12, 200, 900], id="MasterID-TextWideBold"),
        createGSFontMaster(axes=[60, 50, 100], id="MasterID-PosterCondLight"),
        createGSFontMaster(axes=[60, 50, 400], id="MasterID-PosterCondRegular"),
        createGSFontMaster(axes=[60, 50, 900], id="MasterID-PosterCondBold"),
        createGSFontMaster(axes=[60, 200, 100], id="MasterID-PosterWideLight"),
        createGSFontMaster(axes=[60, 200, 400], id="MasterID-PosterWideRegular"),
        createGSFontMaster(axes=[60, 200, 900], id="MasterID-PosterWideBold"),
    ]
    gsFont.glyphs.append(
        createGSGlyph(
            name="A",
            unicodes=[
                0x0041,
            ],
            layers=[GSLayer()],
        )
    )
    return gsFont


async def test_getSourceFromLayerName(testFont):
    glyph = await testFont.getGlyph("a")
    glyphSource = getSourceFromLayerName(
        glyph.sources, "1FA54028-AD2E-4209-AA7B-72DF2DF16264"
    )
    assert glyphSource.location == {"Weight": 155}


expectedLocations = [
    # gsLayerId, expectedFontLocation, expectedGlyphLocation
    [
        "C4872ECA-A3A9-40AB-960A-1DB2202F16DE",
        {"Weight": 17},
        {"crotchDepth": 0, "shoulderWidth": 100},
    ],
    [
        "7C8F98EE-D140-44D5-86AE-E00A730464C0",
        {"Weight": 17},
        {"crotchDepth": -100, "shoulderWidth": 100},
    ],
    [
        "BA4F7DF9-9552-48BB-A5B8-E2D21D8D086E",
        {"Weight": 220},
        {"crotchDepth": -100, "shoulderWidth": 100},
    ],
]


@pytest.mark.parametrize(
    "gsLayerId,expectedFontLocation,expectedGlyphLocation", expectedLocations
)
async def test_splitLocation(
    testFont, gsLayerId, expectedFontLocation, expectedGlyphLocation
):
    glyph = await testFont.getGlyph("_part.shoulder")
    fontSources = await testFont.getSources()
    glyphSource = getSourceFromLayerName(glyph.sources, gsLayerId)
    fontLocation = fontSources[glyphSource.locationBase].location
    fontLocation, glyphLocation = splitLocation(
        fontLocation | glyphSource.location, glyph.axes
    )
    glyphLocation = makeDenseLocation(
        glyphLocation, {axis.name: axis.defaultValue for axis in glyph.axes}
    )
    assert fontLocation == expectedFontLocation
    assert glyphLocation == expectedGlyphLocation


@pytest.mark.parametrize("path", [glyphs3Path])
def test_roundtripGlyphsFileDumps(path):
    root = openstep_plist.loads(path.read_text(), use_numbers=True)
    rawData = convertMatchesToTuples(root, matchTreeFont)

    out = openstepPlistDumps(rawData)
    for root_line, out_line in zip(path.read_text().splitlines(), out.splitlines()):
        assert root_line == out_line


def getWritableTestFont(tmpdir, srcPath):
    dstPath = tmpdir / os.path.basename(srcPath)
    if os.path.isdir(srcPath):
        shutil.copytree(srcPath, dstPath)
    else:
        shutil.copy(srcPath, dstPath)
    return getFileSystemBackend(dstPath)


@pytest.mark.parametrize("path", [glyphsPackagePath, glyphs3Path])
async def test_roundtripGlyphsFile(tmpdir, path):
    testFont = getWritableTestFont(tmpdir, path)
    glyphMap = await testFont.getGlyphMap()

    # open and save same glyphs without changes
    for glyphName in expectedGlyphMap.keys():
        glyph = await testFont.getGlyph(glyphName)
        await testFont.putGlyph(glyphName, glyph, glyphMap[glyphName])

    if os.path.isdir(path):
        # If path is a dir, it's a glyphs package.
        for path, path2 in zip(
            getListOfGlyphsPackageFiles(path),
            getListOfGlyphsPackageFiles(testFont.gsFilePath),
        ):
            compareFilesByLines(path, path2)
    else:
        compareFilesByLines(path, testFont.gsFilePath)


def compareFilesByLines(pathA, pathB):
    linesA = pathA.read_text().splitlines()
    linesB = pathB.read_text().splitlines()

    skipWords = [
        "kernTop",
        "kernBottom",
        "rememberToMakeCoffee",
        "bottomName",
        "topName",
    ]
    linesA = [line for line in linesA if not any(w in line for w in skipWords)]

    assert linesA == linesB


def getGlyhphsPackageFilePath(path, packageFile):
    packagePath = pathlib.Path(path)
    return packagePath / packageFile


def getListOfGlyphsPackageFiles(path):
    filePathList = [
        getGlyhphsPackageFilePath(path, "fontinfo.plist"),
        getGlyhphsPackageFilePath(path, "order.plist"),
    ]
    filePathList += [
        glyphfile
        for glyphfile in getGlyhphsPackageFilePath(path, "glyphs").glob("*.glyph")
    ]
    return filePathList
