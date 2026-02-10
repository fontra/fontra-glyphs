import asyncio
import hashlib
import io
import logging
import pathlib
import uuid
from collections import OrderedDict, defaultdict
from copy import deepcopy
from os import PathLike
from types import SimpleNamespace
from typing import Any

import glyphsLib
import openstep_plist
from fontra.backends.base import ReadableBaseBackend
from fontra.backends.filewatcher import Change
from fontra.backends.watchable import WatchableBackend
from fontra.core.classes import (
    Anchor,
    Axes,
    Component,
    DiscreteFontAxis,
    FontAxis,
    FontInfo,
    FontSource,
    GlyphAxis,
    GlyphSource,
    Guideline,
    ImageData,
    Kerning,
    Layer,
    LineMetric,
    OpenTypeFeatures,
    StaticGlyph,
    VariableGlyph,
)
from fontra.core.discretevariationmodel import findNearestLocationIndex
from fontra.core.path import PackedPathPointPen
from fontra.core.protocols import WritableFontBackend
from fontra.core.subprocess import runInSubProcess
from fontra.core.threading import runInThread
from fontra.core.varutils import (
    locationToTuple,
    makeDenseLocation,
    mapAxesFromUserSpaceToSourceSpace,
)
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.feaLib.error import FeatureLibError
from fontTools.feaLib.parser import Parser as FeatureParser
from fontTools.misc.transform import DecomposedTransform
from fontTools.ufoLib.filenames import userNameToFileName
from glyphsLib.builder.axes import (
    get_axis_definitions,
    get_regular_master,
    to_designspace_axes,
)
from glyphsLib.builder.smart_components import Pole
from glyphsLib.types import Transform as GSTransform

from .utils import (
    convertMatchesToTuples,
    matchTreeFont,
    matchTreeGlyph,
    openstepPlistDumps,
    openstepPlistFromPath,
    splitLocation,
)

logger = logging.getLogger(__name__)


class GlyphsBackendError(Exception):
    pass


rootInfoNames = [
    "familyName",
    "versionMajor",
    "versionMinor",
]


infoNamesMapping = [
    # (Fontra, Glyphs)
    ("copyright", "copyrights"),
    ("designer", "designers"),
    ("designerURL", "designerURL"),
    ("licenseDescription", "licenses"),
    # ("licenseInfoURL", "licensesURL"),  # Not defined in glyphsLib
    ("manufacturer", "manufacturers"),
    ("manufacturerURL", "manufacturerURL"),
    ("trademark", "trademarks"),
    ("vendorID", "vendorID"),
]

GS_KERN_GROUP_PREFIXES = {
    side: f"@MMK_{side[0].upper()}_" for side in ["left", "right", "top", "bottom"]
}
GS_FORMAT_2_KERN_SIDES = [
    # pair side, glyph side
    ("left", "rightKerningGroup"),
    ("right", "leftKerningGroup"),
    ("top", "bottomKerningGroup"),
    ("bottom", "topKerningGroup"),
]
GS_FORMAT_3_KERN_SIDES = [
    # pair side, glyph side
    ("left", "kernRight"),
    ("right", "kernLeft"),
    ("top", "kernBottom"),
    ("bottom", "kernTop"),
]


invalidFeaturesUserDataKey = "xyz.fontra.invalid-features"


class GlyphsBackend(WatchableBackend, ReadableBaseBackend):
    @classmethod
    def fromPath(cls, path: PathLike) -> WritableFontBackend:
        self = cls()
        self._setupFromPath(path)
        return self

    def __init__(self):
        super().__init__()
        self._writeLock = asyncio.Lock()

    def _setupFromPath(self, path: PathLike) -> None:
        self.path = pathlib.Path(path)

        rawFontData, rawGlyphsData = self._loadFiles()
        self._setupWithRawData(rawFontData, rawGlyphsData)

    def _setupWithRawData(self, rawFontData, rawGlyphsData) -> None:
        gsFont = glyphsLib.classes.GSFont()
        parser = glyphsLib.parser.Parser(current_type=gsFont.__class__)
        parser.parse_into_object(gsFont, rawFontData)

        self.gsFont = gsFont
        self.rawFontData = rawFontData
        self._updateRawGlyphsData(rawGlyphsData)

        dsAxes = [
            dsAxis
            for dsAxis in gsAxesToDesignSpaceAxes(self.gsFont)
            # Ignore axes without any range
            if dsAxis.minimum != dsAxis.maximum
        ]

        self.axisNames = {axis.name for axis in dsAxes}

        self.locationByMasterID = {}
        self.masterIDByLocationTuple = {}
        for master in self.gsFont.masters:
            location = {}
            for axisDef in get_axis_definitions(self.gsFont):
                if axisDef.name in self.axisNames:
                    location[axisDef.name] = axisDef.get_design_loc(master)
            self.locationByMasterID[master.id] = location
            self.masterIDByLocationTuple[locationToTuple(location)] = master.id

        axis: FontAxis | DiscreteFontAxis
        axes: list[FontAxis | DiscreteFontAxis] = []
        for dsAxis in dsAxes:
            axis = FontAxis(
                minValue=dsAxis.minimum,
                defaultValue=dsAxis.default,
                maxValue=dsAxis.maximum,
                label=dsAxis.name,
                name=dsAxis.name,
                tag=dsAxis.tag,
                hidden=dsAxis.hidden,
            )
            if dsAxis.map:
                axis.mapping = [[a, b] for a, b in dsAxis.map]
            axes.append(axis)
        self.axes = axes

        axesSourceSpace = mapAxesFromUserSpaceToSourceSpace(self.axes)
        self.defaultLocation = {
            axis.name: axis.defaultValue for axis in axesSourceSpace
        }

    def _updateRawGlyphsData(self, rawGlyphsData):
        # Fill the glyphs list with dummy placeholder glyphs
        self.gsFont.glyphs = [
            glyphsLib.classes.GSGlyph() for i in range(len(rawGlyphsData))
        ]
        self.rawGlyphsData = rawGlyphsData
        self._updateGlyphNameToIndex()
        self.originalGlyphNameToIndex = dict(self.glyphNameToIndex)
        self.parsedGlyphNames: set[str] = set()
        self.glyphMap, self.kerningGroups = self._readGlyphMapAndKerningGroups()

    def _loadFiles(self) -> tuple[dict[str, Any], list[Any]]:
        rawFontData = openstepPlistFromPath(self.path)

        # We separate the "glyphs" list from the rest, so we can prevent glyphsLib
        # from eagerly parsing all glyphs
        rawGlyphsData = rawFontData["glyphs"]
        rawFontData["glyphs"] = []
        return rawFontData, rawGlyphsData

    def _updateGlyphNameToIndex(self):
        self.glyphNameToIndex = {
            glyphData["glyphname"]: i for i, glyphData in enumerate(self.rawGlyphsData)
        }

    @property
    def _kerningSideAttrs(self):
        return (
            GS_FORMAT_2_KERN_SIDES
            if self.gsFont.format_version == 2
            else GS_FORMAT_3_KERN_SIDES
        )

    def _readGlyphMapAndKerningGroups(
        self,
    ) -> tuple[dict[str, list[int]], dict[str, dict[str, list[str]]]]:
        glyphMap = {}
        kerningGroups: dict = defaultdict(lambda: defaultdict(list))

        for glyphData in self.rawGlyphsData:
            glyphName = glyphData["glyphname"]

            # extract code points
            codePoints = glyphData.get("unicode")
            if codePoints is None:
                codePoints = []
            elif self.gsFont.format_version == 2:
                if isinstance(codePoints, str):
                    codePoints = [
                        int(codePoint, 16) for codePoint in codePoints.split(",")
                    ]
                else:
                    assert isinstance(codePoints, int)
                    # The plist parser turned it into an int, but it was a hex string
                    codePoints = [int(str(codePoints), 16)]
            elif isinstance(codePoints, int):
                codePoints = [codePoints]
            else:
                assert all(isinstance(codePoint, int) for codePoint in codePoints)
            glyphMap[glyphName] = codePoints

            # extract kern groups
            for pairSide, glyphSideAttr in self._kerningSideAttrs:
                groupName = glyphData.get(glyphSideAttr)
                if groupName is not None:
                    kerningGroups[pairSide][groupName].append(glyphName)

        return glyphMap, kerningGroups

    def _updateKerningGroups(self):
        changedGlyphs = set()

        for glyphData in self.rawGlyphsData:
            glyphName = glyphData["glyphname"]

            for pairSide, glyphSideAttr in self._kerningSideAttrs:
                groups = self.kerningGroups.get(pairSide)
                currentGroupName = glyphData.get(glyphSideAttr)
                newGroupName = None
                for groupName, group in groups.items():
                    if glyphName in group:
                        newGroupName = groupName
                        break
                if currentGroupName != newGroupName:
                    changedGlyphs.add(glyphName)
                    self.parsedGlyphNames.discard(glyphName)
                    if newGroupName:
                        glyphData[glyphSideAttr] = newGroupName
                    else:
                        glyphData.pop(glyphSideAttr, None)

        return changedGlyphs

    async def getGlyphMap(self) -> dict[str, list[int]]:
        return deepcopy(self.glyphMap)

    async def putGlyphMap(self, value: dict[str, list[int]]) -> None:
        pass

    async def deleteGlyph(self, glyphName: str) -> None:
        if glyphName not in self.glyphNameToIndex:
            logger.debug(f"Can't delete unknown glyph '{glyphName}'")
            return

        del self.glyphMap[glyphName]
        index = self.glyphNameToIndex[glyphName]
        assert self.rawGlyphsData[index]["glyphname"] == glyphName
        del self.rawGlyphsData[index]
        del self.gsFont.glyphs[index]
        self.parsedGlyphNames.discard(glyphName)
        self._updateGlyphNameToIndex()
        self._updateDeletedGlyph(glyphName)

    async def getFontInfo(self) -> FontInfo:
        infoDict = {}
        for name in rootInfoNames:
            value = getattr(self.gsFont, name, None)
            if value is not None:
                infoDict[name] = value

        properties = {p.key: p.value for p in self.gsFont.properties}
        for fontraName, glyphsName in infoNamesMapping:
            value = properties.get(glyphsName)
            if value is not None:
                infoDict[fontraName] = value

        return FontInfo(**infoDict)

    async def putFontInfo(self, fontInfo: FontInfo):
        raise NotImplementedError(
            "GlyphsApp Backend: Editing FontInfo is not yet implemented."
        )

    async def getSources(self) -> dict[str, FontSource]:
        return gsMastersToFontraFontSources(self.gsFont, self.locationByMasterID)

    async def putSources(self, sources: dict[str, FontSource]) -> None:
        raise NotImplementedError(
            "GlyphsApp Backend: Editing FontSources is not yet implemented."
        )

    async def getAxes(self) -> Axes:
        return Axes(axes=deepcopy(self.axes))

    async def putAxes(self, axes: Axes) -> None:
        raise NotImplementedError(
            "GlyphsApp Backend: Editing Axes is not yet implemented."
        )

    async def getUnitsPerEm(self) -> int:
        return self.gsFont.upm

    async def putUnitsPerEm(self, value: int) -> None:
        raise NotImplementedError(
            "GlyphsApp Backend: Editing UnitsPerEm is not yet implemented."
        )

    @property
    def _verticalKerningAttr(self):
        return "vertKerning" if self.gsFont.format_version == 2 else "kerningVertical"

    async def getKerning(self) -> dict[str, Kerning]:
        # TODO: RTL kerning: https://docu.glyphsapp.com/#GSFont.kerningRTL
        kerningLTR = self._gsKerningToFontraKerning("kerning", "left", "right")
        kerningVertical = self._gsKerningToFontraKerning(
            self._verticalKerningAttr, "top", "bottom"
        )

        kerning = {}
        if kerningLTR.values or kerningLTR.groupsSide1 or kerningLTR.groupsSide2:
            kerning["kern"] = kerningLTR
        if (
            kerningVertical.values
            or kerningVertical.groupsSide1
            or kerningVertical.groupsSide2
        ):
            kerning["vkrn"] = kerningVertical
        return kerning

    async def putKerning(self, kerning: dict[str, Kerning]) -> None:
        async with self._writeLock:
            return await runInThread(self._putKerning, kerning)

    def _putKerning(self, kerning: dict[str, Kerning]) -> None:
        unknownKerningTypes = set(kerning) - set(["kern", "vkrn"])
        if unknownKerningTypes:
            s = ", ".join(sorted(unknownKerningTypes))
            raise GlyphsBackendError(
                f"GlyphsApp Backend: '{s}' kern type(s) not supported."
            )

        self._fontraKerningToGSKerning(kerning.get("kern"), "kerning", "left", "right")
        self._fontraKerningToGSKerning(
            kerning.get("vkrn"), self._verticalKerningAttr, "top", "bottom"
        )

        changedGlyphs = self._updateKerningGroups()
        self._writeFontData(changedGlyphs)

    def _gsKerningToFontraKerning(
        self, kerningAttr: str, side1: str, side2: str
    ) -> Kerning:
        gsPrefix1 = GS_KERN_GROUP_PREFIXES[side1]
        gsPrefix2 = GS_KERN_GROUP_PREFIXES[side2]

        groupsSide1 = deepcopy(dict(self.kerningGroups[side1]))
        groupsSide2 = deepcopy(dict(self.kerningGroups[side2]))

        sourceIdentifiers = []
        valueDicts: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))

        defaultMasterID = get_regular_master(self.gsFont).id

        for gsMaster in self.gsFont.masters:
            kernDict = getattr(self.gsFont, kerningAttr, {}).get(gsMaster.id, {})
            if not kernDict and gsMaster.id != defaultMasterID:
                # Even if the default master does not contain kerning, it makes life
                # easier down the road if we include this empty kerning, lest we run
                # into "missing base master"-type interpolation errors.
                continue

            sourceIdentifiers.append(gsMaster.id)

            for name1, name2Dict in kernDict.items():
                name1 = translateGroupName(name1, gsPrefix1, "@")

                for name2, value in name2Dict.items():
                    name2 = translateGroupName(name2, gsPrefix2, "@")
                    valueDicts[name1][name2][gsMaster.id] = value

        values = {
            left: {
                right: [valueDict.get(key) for key in sourceIdentifiers]
                for right, valueDict in rightDict.items()
            }
            for left, rightDict in valueDicts.items()
        }

        return Kerning(
            groupsSide1=groupsSide1,
            groupsSide2=groupsSide2,
            sourceIdentifiers=sourceIdentifiers,
            values=values,
        )

    def _fontraKerningToGSKerning(
        self, kerning: Kerning | None, kerningAttr: str, side1: str, side2: str
    ) -> None:
        if kerning is None:
            setattr(self.gsFont, kerningAttr, {})
            self.kerningGroups[side1].clear()
            self.kerningGroups[side2].clear()
            return

        if kerningAttr == "vertKerning":
            raise GlyphsBackendError(
                "Writing vertical kerning is not supported for the Glyphs 2 format"
            )

        sourceIdentifiers = kerning.sourceIdentifiers
        unknownSourceIdentifiers = set(sourceIdentifiers) - set(
            gsMaster.id for gsMaster in self.gsFont.masters
        )

        if unknownSourceIdentifiers:
            s = ", ".join(sorted(unknownSourceIdentifiers))
            raise GlyphsBackendError(
                f"Can't write kerning, found unknown source identifiers: {s}"
            )

        gsPrefix1 = GS_KERN_GROUP_PREFIXES[side1]
        gsPrefix2 = GS_KERN_GROUP_PREFIXES[side2]
        kerningPerSource: dict = defaultdict(lambda: defaultdict(dict))

        for leftName, rightDict in kerning.values.items():
            if leftName.startswith("@"):
                leftName = gsPrefix1 + leftName[1:]
            for rightName, values in rightDict.items():
                if rightName.startswith("@"):
                    rightName = gsPrefix2 + rightName[1:]
                for sourceIdentifier, value in zip(sourceIdentifiers, values):
                    if value is not None:
                        kerningPerSource[sourceIdentifier][leftName][rightName] = value

        kerningPerSource = OrderedDict(
            {
                gsMaster.id: dict(kerningPerSource.get(gsMaster.id, {}))
                for gsMaster in self.gsFont.masters
            }
        )

        setattr(self.gsFont, kerningAttr, kerningPerSource)

        self.kerningGroups[side1] = deepcopy(kerning.groupsSide1)
        self.kerningGroups[side2] = deepcopy(kerning.groupsSide2)

    async def getFeatures(self) -> OpenTypeFeatures:
        invalidFeatures = self.gsFont.userData.get(invalidFeaturesUserDataKey)
        if invalidFeatures is not None:
            return OpenTypeFeatures(text=invalidFeatures)

        featureText = glyphsLib.builder.features._to_ufo_features(self.gsFont)
        if not canParseFeatures(featureText, self.glyphNameToIndex.keys()):
            expandedFeatures = await runInSubProcess(expensiveGetFeatures, self.path)
            if expandedFeatures:
                featureText = expandedFeatures

        return OpenTypeFeatures(text=featureText)

    async def putFeatures(self, features: OpenTypeFeatures) -> None:
        async with self._writeLock:
            return await runInThread(self._putFeatures, features)

    def _putFeatures(self, features: OpenTypeFeatures) -> None:
        if features.language != "fea":
            raise NotImplementedError(
                "GlyphsApp Backend: skip writing features in unsupported language: "
                f"{features.language!r}"
            )

        # Delete existing features, prefixes and classes
        # This is needed, because glyphsLib.builder.features._to_glyphs_features()
        # will not overwrite existing features, prefixes and classes.
        # '_to_glyphs_features' only adds new ones to self.gsFont.
        self.gsFont.featurePrefixes = []
        self.gsFont.features = []
        self.gsFont.classes = []

        # Convert feature.text into Glyphs featurePrefixes, features and classes
        try:
            glyphsLib.builder.features._to_glyphs_features(
                self.gsFont, features.text, glyph_names=self.glyphNameToIndex.keys()
            )
        except FeatureLibError:
            self.gsFont.userData[invalidFeaturesUserDataKey] = features.text
        else:
            if invalidFeaturesUserDataKey in self.gsFont.userData:
                del self.gsFont.userData[invalidFeaturesUserDataKey]

        self._writeFontData()

    def _writeFontData(self, changedGlyphs=None):
        # Set self.gsFont.glyphs to an empty list temporarily, so no time is wasted on these.
        originalGlyphs = self.gsFont.glyphs
        self.gsFont.glyphs = []
        try:
            self.rawFontData = self._getRawData(self.gsFont)
            self._writeRawFontData(changedGlyphs)
        finally:
            self.gsFont.glyphs = originalGlyphs

    async def getBackgroundImage(self, imageIdentifier: str) -> ImageData | None:
        return None

    async def putBackgroundImage(self, imageIdentifier: str, data: ImageData) -> None:
        raise NotImplementedError(
            "GlyphsApp Backend: Editing BackgroundImage is not yet implemented."
        )

    async def getCustomData(self) -> dict[str, Any]:
        return {}

    async def putCustomData(self, lib):
        raise NotImplementedError(
            "GlyphsApp Backend: Editing CustomData is not yet implemented."
        )

    async def getGlyph(self, glyphName: str) -> VariableGlyph | None:
        if glyphName not in self.glyphNameToIndex:
            return None

        self._ensureGlyphIsParsed(glyphName)

        gsGlyph = self.gsFont.glyphs[glyphName]

        customData = {}
        if gsGlyph.color is not None:
            customData["com.glyphsapp.glyph-color"] = gsGlyph.color

        localAxes = gsLocalAxesToFontraLocalAxes(gsGlyph)
        localAxesByName = {axis.name: axis for axis in localAxes}
        sources = []
        layers = {}

        seenMasterIDs: dict[str, None] = {}
        gsLayers = []
        for i, gsLayer in enumerate(gsGlyph.layers):
            gsLayers.append((i, gsLayer))
            assert gsLayer.associatedMasterId
            # We use a dict as a set, because we need the insertion order
            seenMasterIDs[gsLayer.associatedMasterId] = None

        masterOrder = {masterID: i for i, masterID in enumerate(seenMasterIDs)}
        gsLayers = sorted(
            gsLayers, key=lambda i_gsLayer: masterOrder[i_gsLayer[1].associatedMasterId]
        )

        seenLocations = []
        for i, gsLayer in gsLayers:
            braceLocation = self._getBraceLayerLocation(gsLayer)
            smartLocation = self._getSmartLocation(gsLayer, localAxesByName)
            masterName = self.gsFont.masters[gsLayer.associatedMasterId].name
            if "xyz.fontra.source-name" in gsLayer.userData:
                sourceName = gsLayer.userData["xyz.fontra.source-name"]
            elif braceLocation or smartLocation:
                sourceName = f"{masterName} / {gsLayer.name}"
            else:
                sourceName = gsLayer.name or masterName
            layerName = gsLayer.userData["xyz.fontra.layer-name"] or gsLayer.layerId

            baseLocation = self.locationByMasterID[gsLayer.associatedMasterId]
            extraLocation = braceLocation | smartLocation
            location = baseLocation | extraLocation
            needLocationBase = not (set(extraLocation) >= set(self.defaultLocation))

            storeLayerId = True
            if location in seenLocations:
                layerName = f"{gsLayer.associatedMasterId}^{gsLayer.name}"
                bgSeparator = "/"
            else:
                storeLayerId = layerName != gsLayer.layerId
                seenLocations.append(location)
                sources.append(
                    GlyphSource(
                        name=(
                            sourceName
                            if extraLocation or sourceName != masterName
                            else ""
                        ),
                        location=extraLocation,
                        locationBase=(
                            gsLayer.associatedMasterId if needLocationBase else None
                        ),
                        layerName=layerName,
                    )
                )
                bgSeparator = "^"

            layers[layerName] = gsLayerToFontraLayer(
                gsLayer,
                self.axisNames,
                gsLayer.width,
                gsLayer.layerId if storeLayerId else None,
            )

            if gsLayer.hasBackground:
                layers[layerName + bgSeparator + "background"] = gsLayerToFontraLayer(
                    gsLayer.background, self.axisNames, gsLayer.width, None
                )

        fixSourceLocations(sources, set(smartLocation))

        glyph = VariableGlyph(
            name=glyphName,
            axes=localAxes,
            sources=sources,
            layers=layers,
            customData=customData,
        )
        return glyph

    def _ensureGlyphIsParsed(self, glyphName: str) -> None:
        if glyphName in self.parsedGlyphNames:
            return

        glyphIndex = self.glyphNameToIndex[glyphName]
        rawGlyphData = self.rawGlyphsData[glyphIndex]
        self.parsedGlyphNames.add(glyphName)

        gsGlyph = glyphsLib.classes.GSGlyph()
        p = glyphsLib.parser.Parser(
            current_type=gsGlyph.__class__, format_version=self.gsFont.format_version
        )
        p.parse_into_object(gsGlyph, rawGlyphData)
        assert glyphIndex < len(self.gsFont.glyphs)
        self.gsFont.glyphs[glyphIndex] = gsGlyph

        # Load all component dependencies
        componentNames = set()
        for layer in gsGlyph.layers:
            for component in layer.components:
                componentNames.add(component.name)
            if layer.hasBackground:
                for component in layer.background.components:
                    componentNames.add(component.name)

        for compoName in sorted(componentNames):
            if compoName not in self.glyphNameToIndex:
                continue
            self._ensureGlyphIsParsed(compoName)

    def _getBraceLayerLocation(self, gsLayer):
        if not gsLayer._is_brace_layer():
            return {}

        return dict(
            (axis.name, value)
            for axis, value in zip(self.axes, gsLayer._brace_coordinates())
        )

    def _getSmartLocation(self, gsLayer, localAxesByName):
        location = {
            name: (
                localAxesByName[name].minValue
                if poleValue == Pole.MIN
                else localAxesByName[name].maxValue
            )
            for name, poleValue in gsLayer.smartComponentPoleMapping.items()
        }
        return {
            disambiguateLocalAxisName(name, self.axisNames): value
            for name, value in location.items()
            if value != localAxesByName[name].defaultValue
        }

    def _getRawData(self, object):
        # Serialize to text with glyphsLib.writer.Writer(), using io.StringIO
        f = io.StringIO()
        writer = glyphsLib.writer.Writer(f)
        writer.format_version = self.gsFont.format_version
        writer.write(object)

        # Parse stream into "raw" object
        return openstep_plist.loads(f.getvalue(), use_numbers=True)

    async def putGlyph(
        self, glyphName: str, glyph: VariableGlyph, codePoints: list[int]
    ) -> None:
        async with self._writeLock:
            return await runInThread(self._putGlyph, glyphName, glyph, codePoints)

    def _putGlyph(
        self, glyphName: str, glyph: VariableGlyph, codePoints: list[int]
    ) -> None:
        assert isinstance(codePoints, list)
        assert all(isinstance(cp, int) for cp in codePoints)
        assert all(source.layerName in glyph.layers for source in glyph.sources)

        self.glyphMap[glyphName] = codePoints

        isNewGlyph = glyphName not in self.gsFont.glyphs
        # Glyph does not exist: create new one.
        if isNewGlyph:
            gsGlyph = glyphsLib.classes.GSGlyph(glyphName)
            self.gsFont.glyphs.append(gsGlyph)
            self.glyphNameToIndex[glyphName] = len(self.gsFont.glyphs) - 1

        gsGlyph = deepcopy(self.gsFont.glyphs[glyphName])

        self._variableGlyphToGSGlyph(glyph, gsGlyph)

        # Update unicodes: need to be converted from decimal to hex strings
        gsGlyph.unicodes = [f"{codePoint:04X}" for codePoint in codePoints]

        rawGlyphData = self._getRawData(gsGlyph)
        self._updateKerningSidesForGlyph(rawGlyphData)

        # Replace original "raw" object with new "raw" object
        glyphIndex = self.glyphNameToIndex[glyphName]
        if isNewGlyph:
            assert glyphIndex == len(self.rawGlyphsData)
            self.rawGlyphsData.append(rawGlyphData)
            self.rawGlyphsData.sort(
                key=lambda glyphData: self.originalGlyphNameToIndex.get(
                    glyphData["glyphname"], 0xFFFFFFFF
                )
            )
            self._updateGlyphNameToIndex()
        else:
            self.rawGlyphsData[glyphIndex] = rawGlyphData

        self._writeRawGlyph(glyphName, isNewGlyph)

        # Remove glyph from parsed glyph names, because we changed it.
        # Next time it needs to be parsed again.
        self.parsedGlyphNames.discard(glyphName)

    def _variableGlyphToGSGlyph(self, variableGlyph, gsGlyph):
        sourceLayers, sourceLayerNames = getSourceLayerNames(variableGlyph)
        nonSourceLayerNames = set(variableGlyph.layers) - sourceLayerNames

        if nonSourceLayerNames:
            raise GlyphsBackendError(
                "GlyphsApp Backend: Layer without glyph source is not supported."
            )

        defaultGlyphLocation = getDefaultLocation(variableGlyph.axes)

        gsGlyph.smartComponentAxes = setupSmartComponentAxes(variableGlyph)

        layerIdsInUse = set()

        for glyphSource in variableGlyph.sources:
            sourceInfo = self._setupSourceInfo(
                glyphSource, sourceLayers, variableGlyph, defaultGlyphLocation
            )

            sourceLayerNames = [glyphSource.layerName] + sorted(
                sourceLayers[glyphSource.layerName]
            )

            for layerName in sourceLayerNames:
                assert layerName in variableGlyph.layers

                layerInfo = setupLayerInfo(
                    glyphSource, sourceInfo, layerName, variableGlyph, gsGlyph
                )

                gsLayer = getOrCreateGSLayer(gsGlyph, layerInfo.gsLayerId)
                layerIdsInUse.add(layerInfo.gsLayerId)

                targetLayer = updateGSLayer(
                    variableGlyph,
                    layerName,
                    glyphSource,
                    gsLayer,
                    sourceInfo,
                    layerInfo,
                )

                fontraLayerToGSLayer(variableGlyph.layers[layerName], targetLayer)

                if sourceInfo.isBraceLayer:
                    gsLayer.attributes["coordinates"] = list(
                        sourceInfo.fontLocation.values()
                    )

        for gsLayer in list(gsGlyph.layers):
            if gsLayer.layerId not in layerIdsInUse:
                del gsGlyph.layers[gsLayer.layerId]

    def _setupSourceInfo(
        self, glyphSource, sourceLayers, variableGlyph, defaultGlyphLocation
    ):
        fontLocation, glyphLocation = self._getSourceLocations(
            glyphSource, variableGlyph.axes, defaultGlyphLocation
        )

        masterId = self.masterIDByLocationTuple.get(locationToTuple(fontLocation))
        masterName = (
            self.gsFont.masters[masterId].name if masterId is not None else None
        )
        isBraceLayer = masterId is None
        isSmartComponentLayer = glyphLocation != defaultGlyphLocation

        if isBraceLayer and variableGlyph.axes:
            raise NotImplementedError(
                "GlyphsApp Backend: Brace layers "
                "within smart glyphs are not yet implemented."
            )

        associatedMasterId = (
            masterId
            or glyphSource.customData.get("com.glyphsapp.layer.associatedMasterId")
            or self._findNearestMasterId(fontLocation)
        )
        associatedMasterName = self.gsFont.masters[associatedMasterId].name

        return SimpleNamespace(
            fontLocation=fontLocation,
            glyphLocation=glyphLocation,
            masterId=masterId,
            masterName=masterName,
            isBraceLayer=isBraceLayer,
            isSmartComponentLayer=isSmartComponentLayer,
            associatedMasterId=associatedMasterId,
            associatedMasterName=associatedMasterName,
        )

    def _getSourceLocations(self, glyphSource, glyphAxes, defaultGlyphLocation):
        location = self._getSourceLocation(glyphSource)
        fontLocation, glyphLocation = splitLocation(location, glyphAxes)
        fontLocation = makeDenseLocation(fontLocation, self.defaultLocation)
        glyphLocation = makeDenseLocation(glyphLocation, defaultGlyphLocation)
        return fontLocation, glyphLocation

    def _getSourceLocation(self, glyphSource):
        baseLocation = (
            {}
            if glyphSource.locationBase is None
            else self.locationByMasterID[glyphSource.locationBase]
        )
        return baseLocation | glyphSource.location

    def _updateKerningSidesForGlyph(self, rawGlyphData):
        glyphName = rawGlyphData["glyphname"]
        for pairSide, glyphSideAttr in self._kerningSideAttrs:
            if pairSide not in self.kerningGroups:
                continue
            for groupName, glyphNames in self.kerningGroups[pairSide].items():
                if glyphName in glyphNames:
                    rawGlyphData[glyphSideAttr] = groupName

        # sort dict by key for easier round-tripping
        sortedData = {k: v for k, v in sorted(rawGlyphData.items())}
        rawGlyphData.clear()
        rawGlyphData.update(sortedData)

    def _writeRawFontData(self, changedGlyphs=None):
        # `changedGlyphs` is ignored, needed for glyphsPackage
        rawFontData = dict(self.rawFontData)
        rawFontData["glyphs"] = self.rawGlyphsData

        rawFontData = convertMatchesToTuples(rawFontData, matchTreeFont)
        out = openstepPlistDumps(rawFontData)
        self.path.write_text(out)
        self.fileWatcherIgnoreNextChange(self.path)

    def _writeRawGlyph(self, glyphName, isNewGlyph):
        # Write whole file with openstep_plist
        # 'glyphName' and 'isNewGlyph' arguments not used, because we write the whole file,
        # but is required for the glyphspackage backend
        self._writeRawFontData()

    def _updateDeletedGlyph(self, glyphName):
        # `glyphName` is ignored, needed for glyphsPackage
        self._writeRawFontData()

    def _findNearestMasterId(self, fontLocation):
        masterIDs = list(self.locationByMasterID)
        locations = list(self.locationByMasterID.values())
        index = findNearestLocationIndex(fontLocation, locations)
        return masterIDs[index]

    async def aclose(self) -> None:
        pass

    async def findGlyphsThatUseGlyph(self, glyphName: str) -> list[str]:
        componentsKey = "components" if self.gsFont.format_version == 2 else "shapes"
        baseGlyphKey = "name" if self.gsFont.format_version == 2 else "ref"

        usedBy = set()

        for glyphData in self.rawGlyphsData:
            for layerData in glyphData["layers"]:
                for compo in layerData.get(componentsKey, []):
                    baseGlyph = compo.get(baseGlyphKey)
                    if baseGlyph == glyphName:
                        usedBy.add(glyphData["glyphname"])

        return sorted(usedBy)

    def fileWatcherWasInstalled(self):
        self.fileWatcher.setPaths([self.path])

    async def fileWatcherProcessChanges(
        self, changes: set[tuple[Change, str]]
    ) -> dict[str, Any] | None:
        reloadPattern: dict[str, Any] = {}
        glyphChanges = set()

        rawFontData, rawGlyphsData = self._loadFiles()

        if rawFontData != self.rawFontData:
            self._setupWithRawData(rawFontData, rawGlyphsData)
            # Reload everything
            return None

        if rawGlyphsData != self.rawGlyphsData:
            oldGlyphs = {
                glyphData["glyphname"]: glyphData for glyphData in self.rawGlyphsData
            }
            newGlyphs = {
                glyphData["glyphname"]: glyphData for glyphData in rawGlyphsData
            }
            oldGlyphNames = set(oldGlyphs)
            newGlyphNames = set(newGlyphs)
            glyphSetChanges = oldGlyphNames ^ newGlyphNames
            glyphMapChanged = bool(glyphSetChanges)
            glyphChanges = {
                glyphName
                for glyphName in oldGlyphNames & newGlyphNames
                if oldGlyphs[glyphName] != newGlyphs[glyphName]
            }
            if not glyphMapChanged:
                glyphMapChanged = any(
                    oldGlyphs[glyphName].get("unicode")
                    != newGlyphs[glyphName].get("unicode")
                    for glyphName in glyphChanges
                )

            if glyphSetChanges or glyphChanges:
                reloadPattern["glyphs"] = dict.fromkeys(
                    sorted(glyphSetChanges | glyphChanges)
                )

            if glyphMapChanged:
                reloadPattern["glyphMap"] = None

            if glyphChanges or glyphMapChanged:
                self._updateRawGlyphsData(rawGlyphsData)

        return reloadPattern


def getSourceLayerNames(variableGlyph):
    sourceLayers = {
        source.layerName: [
            layerName
            for layerName in variableGlyph.layers
            if layerName.startswith(source.layerName + "^")
        ]
        for source in variableGlyph.sources
    }

    for source in variableGlyph.sources:
        assert source.layerName in variableGlyph.layers, source.layerName

    for layerName, layerNames in sourceLayers.items():
        assert layerName in variableGlyph.layers, layerName
        for ln in layerNames:
            assert ln in variableGlyph.layers

    sourceLayerNames = set(sourceLayers) | {
        layerName for layerNames in sourceLayers.values() for layerName in layerNames
    }
    return sourceLayers, sourceLayerNames


def getDefaultLocation(axes):
    return {axis.name: axis.defaultValue for axis in axes}


def setupLayerInfo(glyphSource, sourceInfo, layerName, variableGlyph, gsGlyph):
    isMainLayer = layerName == glyphSource.layerName
    shouldStoreFontraSourceName = glyphSource.name != sourceInfo.associatedMasterName
    shouldStoreFontraLayerName = True

    if isMainLayer:
        if sourceInfo.isSmartComponentLayer:
            gsLayerName = glyphSource.name
            gsLayerId = getLayerId(variableGlyph, layerName, None)
        else:
            gsLayerId = getLayerId(variableGlyph, layerName, sourceInfo.masterId)
            gsLayerName = (
                getBraceLayerName(sourceInfo.fontLocation)
                if sourceInfo.isBraceLayer
                else sourceInfo.masterName
            )
        if " / " in glyphSource.name:
            masterName, sourceName = glyphSource.name.split(" / ", 1)
            if masterName == sourceInfo.associatedMasterName:
                gsLayerName = sourceName
                shouldStoreFontraSourceName = False
    else:
        _, localLayerName = layerName.split("^", 1)
        if localLayerName == "background" or localLayerName.endswith("/background"):
            baseLayerName = layerName[:-11]  # minus "^background" or "/background"
            gsLayerName = None
            gsLayerId = getLayerId(
                variableGlyph,
                baseLayerName,
                (sourceInfo.masterId if localLayerName == "background" else None),
            )
        else:
            gsLayerName = localLayerName
            gsLayerId = getLayerId(variableGlyph, layerName, None)
            shouldStoreFontraLayerName = False
            if gsLayerId not in gsGlyph.layers:
                if sourceInfo.isBraceLayer:
                    raise GlyphsBackendError(
                        "A brace layer can only have an additional source "
                        "layer named 'background'"
                    )

    return SimpleNamespace(
        isMainLayer=isMainLayer,
        isBackgroundLayer=gsLayerName is None,
        gsLayerName=gsLayerName,
        gsLayerId=gsLayerId,
        shouldStoreFontraSourceName=shouldStoreFontraSourceName,
        shouldStoreFontraLayerName=shouldStoreFontraLayerName,
    )


def getLayerId(variableGlyph, layerName, suggestedLayerId):
    layer = variableGlyph.layers[layerName]
    layerId = layer.customData.get("com.glyphsapp.layer.layerId")

    if layerId is None:
        layerId = suggestedLayerId

    if layerId is None:
        if isGlyphsUUID(layerName):
            layerId = layerName
        else:
            seed = f"{variableGlyph.name}/{layerName}".encode("utf-8")
            h = hashlib.sha1(seed).digest()
            layerId = str(uuid.UUID(bytes=h[:16])).upper()

    return layerId


def getOrCreateGSLayer(gsGlyph, gsLayerId):
    gsLayer = gsGlyph.layers[gsLayerId]
    if gsLayer is None:
        gsLayer = glyphsLib.classes.GSLayer()
        gsLayer.layerId = gsLayerId
        gsLayer.parent = gsGlyph
        gsGlyph.layers.append(gsLayer)
    return gsLayer


def updateGSLayer(
    variableGlyph,
    layerName,
    glyphSource,
    gsLayer,
    sourceInfo,
    layerInfo,
):
    if layerInfo.isBackgroundLayer:
        return gsLayer.background

    assert gsLayer.layerId == layerInfo.gsLayerId
    gsLayer.name = layerInfo.gsLayerName
    gsLayer.associatedMasterId = sourceInfo.associatedMasterId
    if layerInfo.isMainLayer and variableGlyph.axes:
        gsLayer.smartComponentPoleMapping = setupPoleMapping(
            variableGlyph.axes, sourceInfo.glyphLocation
        )

    storeInDict(
        gsLayer.userData,
        "xyz.fontra.layer-name",
        layerName,
        layerName != layerInfo.gsLayerId and layerInfo.shouldStoreFontraLayerName,
    )

    storeInDict(
        gsLayer.userData,
        "xyz.fontra.source-name",
        glyphSource.name,
        glyphSource.name and layerInfo.shouldStoreFontraSourceName,
    )

    return gsLayer


def isGlyphsUUID(maybeUUID):
    try:
        u = uuid.UUID(maybeUUID)
    except ValueError:
        return False
    else:
        return maybeUUID == str(u).upper()


def getBraceLayerName(location):
    values = [str(makeIntIfInt(value)) for value in location.values()]
    return f"{{{','.join(values)}}}"


def makeIntIfInt(n):
    if isinstance(n, int):
        return n
    return int(n) if n.is_integer() else n


def storeInDict(d, key, value, doStore):
    if doStore:
        d[key] = value
    elif key in d:
        # can't use d.pop(k, None) because glyphsLib's pop doesn't support it
        del d[key]


class GlyphsPackageBackend(GlyphsBackend):
    fontInfoFileName = "fontinfo.plist"
    orderFileName = "order.plist"
    glyphsFolderName = "glyphs"

    @property
    def fontInfoPath(self):
        return self.path / self.fontInfoFileName

    @property
    def orderPath(self):
        return self.path / self.orderFileName

    @property
    def glyphsPath(self):
        return self.path / self.glyphsFolderName

    def _loadFiles(self) -> tuple[dict[str, Any], list[Any]]:
        glyphOrder = []
        if self.orderPath.exists():
            glyphOrder = openstepPlistFromPath(self.orderPath)
        glyphNameToIndex = {glyphName: i for i, glyphName in enumerate(glyphOrder)}

        rawFontData = openstepPlistFromPath(self.fontInfoPath)
        rawFontData["glyphs"] = []

        rawGlyphsData = []
        for glyphPath in self.glyphsPath.glob("*.glyph"):
            glyphData = openstepPlistFromPath(glyphPath)
            rawGlyphsData.append(glyphData)

        rawGlyphsData.sort(
            key=lambda glyphData: (
                glyphNameToIndex.get(glyphData["glyphname"], 0xFFFFFFFF),
                glyphData["glyphname"],
            )
        )

        return rawFontData, rawGlyphsData

    def _writeRawFontData(self, changedGlyphs=None):
        rawFontData = convertMatchesToTuples(self.rawFontData, matchTreeFont)

        # There can be an empty glyphs list at this point, which we don't want
        # to write
        rawFontData.pop("glyphs", None)

        out = openstepPlistDumps(rawFontData)
        self.fontInfoPath.write_text(out, encoding="utf=8")
        self.fileWatcherIgnoreNextChange(self.fontInfoPath)

        if changedGlyphs:
            for glyphName in sorted(changedGlyphs):
                self._writeRawGlyph(glyphName, False)

    def _writeRawGlyph(self, glyphName, isNewGlyph):
        rawGlyphData = self.rawGlyphsData[self.glyphNameToIndex[glyphName]]
        rawGlyphData = convertMatchesToTuples(rawGlyphData, matchTreeGlyph)
        out = openstepPlistDumps(rawGlyphData)
        filePath = self.getGlyphFilePath(glyphName)
        filePath.write_text(out, encoding="utf=8")
        self.fileWatcherIgnoreNextChange(filePath)

        if isNewGlyph:
            self._updateGlyphOrder()

    def _updateDeletedGlyph(self, glyphName):
        filePath = self.getGlyphFilePath(glyphName)
        filePath.unlink()
        self.fileWatcherIgnoreNextChange(filePath)
        self._updateGlyphOrder()

    def _updateGlyphOrder(self):
        glyphOrder = [glyph["glyphname"] for glyph in self.rawGlyphsData]
        out = openstepPlistDumps(glyphOrder)
        self.orderPath.write_text(out, encoding="utf=8")
        self.fileWatcherIgnoreNextChange(self.orderPath)

    def getGlyphFilePath(self, glyphName):
        refFileName = userNameToFileName(glyphName, suffix=".glyph")
        return self.glyphsPath / refFileName


def gsLayerToFontraLayer(gsLayer, globalAxisNames, gsLayerWidth, gsLayerId):
    pen = PackedPathPointPen()
    gsLayer.drawPoints(pen)

    components = [
        gsComponentToFontraComponent(gsComponent, gsLayer, globalAxisNames)
        for gsComponent in gsLayer.components
    ]

    anchors = [gsAnchorToFontraAnchor(gsAnchor) for gsAnchor in gsLayer.anchors]
    guidelines = [
        gsGuidelineToFontraGuideline(gsGuideline) for gsGuideline in gsLayer.guides
    ]

    customData = (
        {"com.glyphsapp.layer.layerId": gsLayerId} if gsLayerId is not None else {}
    )

    return Layer(
        glyph=StaticGlyph(
            xAdvance=gsLayerWidth,
            path=pen.getPath(),
            components=components,
            anchors=anchors,
            guidelines=guidelines,
        ),
        customData=customData,
    )


def gsComponentToFontraComponent(gsComponent, gsLayer, globalAxisNames):
    component = Component(
        name=gsComponent.name,
        transformation=DecomposedTransform.fromTransform(gsComponent.transform),
        location={
            disambiguateLocalAxisName(name, globalAxisNames): value
            for name, value in gsComponent.smartComponentValues.items()
        },
    )
    if gsComponent.alignment:
        # The aligment can be 0, but in that case, do not set it.
        component.customData["com.glyphsapp.component.alignment"] = (
            gsComponent.alignment
        )
    return component


def disambiguateLocalAxisName(axisName, globalAxisNames):
    return f"{axisName} (local)" if axisName in globalAxisNames else axisName


def gsAnchorToFontraAnchor(gsAnchor):
    anchor = Anchor(
        name=gsAnchor.name,
        x=gsAnchor.position.x,
        y=gsAnchor.position.y,
        # TODO: gsAnchor.orientation – If the position of the anchor
        # is relative to the LSB (0), center (2) or RSB (1).
        # Details: https://docu.glyphsapp.com/#GSAnchor.orientation
        customData=dict(gsAnchor.userData) if gsAnchor.userData else dict(),
    )
    return anchor


def gsGuidelineToFontraGuideline(gsGuideline):
    return Guideline(
        x=gsGuideline.position.x,
        y=gsGuideline.position.y,
        angle=gsGuideline.angle,
        name=gsGuideline.name,
        locked=gsGuideline.locked,
    )


class MinimalUFOBuilder:
    def __init__(self, gsFont):
        self.font = gsFont
        self.designspace = DesignSpaceDocument()
        self.minimize_glyphs_diffs = False

    to_designspace_axes = to_designspace_axes


def gsAxesToDesignSpaceAxes(gsFont):
    builder = MinimalUFOBuilder(gsFont)
    builder.to_designspace_axes()
    return builder.designspace.axes


def gsLocalAxesToFontraLocalAxes(gsGlyph):
    basePoleMapping = gsGlyph.layers[0].smartComponentPoleMapping
    return [
        GlyphAxis(
            name=axis.name,
            minValue=axis.bottomValue,
            defaultValue=(
                axis.bottomValue
                if basePoleMapping[axis.name] == Pole.MIN
                else axis.topValue
            ),
            maxValue=axis.topValue,
        )
        for axis in gsGlyph.smartComponentAxes
    ]


def fixSourceLocations(sources, smartAxisNames):
    # If a set of sources is equally controlled by a font axis and a glyph axis
    # (smart axis), then the font axis should be ignored. This makes our
    # varLib-based variation model behave like Glyphs.
    sets = defaultdict(set)
    for i, source in enumerate(sources):
        for locItem in source.location.items():
            sets[locItem].add(i)

    reverseSets = defaultdict(set)
    for locItem, sourceIndices in sets.items():
        reverseSets[tuple(sorted(sourceIndices))].add(locItem)

    matches = [locItems for locItems in reverseSets.values() if len(locItems) > 1]

    locItemsToDelete = []
    for locItems in matches:
        for axis, value in locItems:
            if axis not in smartAxisNames:
                locItemsToDelete.append((axis, value))

    for axis, value in locItemsToDelete:
        for source in sources:
            if source.location.get(axis) == value:
                del source.location[axis]


def translateGroupName(name, oldPrefix, newPrefix):
    return newPrefix + name[len(oldPrefix) :] if name.startswith(oldPrefix) else name


def gsMastersToFontraFontSources(gsFont, locationByMasterID):
    sources = {}
    for gsMaster in gsFont.masters:
        sources[gsMaster.id] = FontSource(
            name=gsMaster.name,
            italicAngle=gsMaster.italicAngle,
            location=locationByMasterID[gsMaster.id],
            lineMetricsHorizontalLayout=gsVerticalMetricsToFontraLineMetricsHorizontal(
                gsFont, gsMaster
            ),
            guidelines=[
                gsGuidelineToFontraGuideline(gsGuideline)
                for gsGuideline in gsMaster.guides
            ],
        )
    return sources


def gsToFontraZone(gsVerticalMetricsValue, gsAlignmentZones):
    for gsZone in gsAlignmentZones:
        if gsZone.position == gsVerticalMetricsValue:
            return gsZone.size
    return 0


def gsVerticalMetricsToFontraLineMetricsHorizontal(gsFont, gsMaster):
    lineMetricsHorizontal = {
        "ascender": LineMetric(
            value=gsMaster.ascender,
            zone=gsToFontraZone(gsMaster.ascender, gsMaster.alignmentZones),
        ),
        "capHeight": LineMetric(
            value=gsMaster.capHeight,
            zone=gsToFontraZone(gsMaster.capHeight, gsMaster.alignmentZones),
        ),
        "xHeight": LineMetric(
            value=gsMaster.xHeight,
            zone=gsToFontraZone(gsMaster.xHeight, gsMaster.alignmentZones),
        ),
        "baseline": LineMetric(
            value=0, zone=gsToFontraZone(0, gsMaster.alignmentZones)
        ),
        "descender": LineMetric(
            value=gsMaster.descender,
            zone=gsToFontraZone(gsMaster.descender, gsMaster.alignmentZones),
        ),
    }

    # TODO: custom metrics https://docu.glyphsapp.com/#GSFontMaster.metrics
    # Custom vertical metrics seem not to work with GlyphsLib, currently.
    # The following code works within GlyphsApp, but not with GlyphsLib.
    # for gsMetric in gsFont.metrics:
    #     if gsMetric.name:
    #         # if it has a name, it is a custom vertical metric
    #         gsMetricValue = gsMaster.metricValues[gsMetric.id]
    #         print('position: ', gsMetricValue.position)
    #         print('overshoot: ', gsMetricValue.overshoot)
    #         lineMetricsHorizontal[gsMetric.name] = LineMetric(
    #             value=gsMetricValue.position,
    #             zone=gsToFontraZone(gsMetricValue.overshoot, gsMaster.alignmentZones)
    #         )

    return lineMetricsHorizontal


def setupSmartComponentAxes(variableGlyph):
    smartComponentAxes = []
    for axis in variableGlyph.axes:
        if axis.defaultValue not in [axis.minValue, axis.maxValue]:
            # NOTE: GlyphsApp does not have axis.defaultValue,
            # therefore it must be at MIN or MAX.
            # https://docu.glyphsapp.com/#GSSmartComponentAxis
            raise GlyphsBackendError(
                f"GlyphsApp Backend: Glyph axis '{axis.name}' "
                "defaultValue must be at MIN or MAX."
            )
        gsAxis = glyphsLib.classes.GSSmartComponentAxis()
        gsAxis.name = axis.name
        gsAxis.bottomValue = axis.minValue
        gsAxis.topValue = axis.maxValue
        smartComponentAxes.append(gsAxis)
    return smartComponentAxes


def setupPoleMapping(glyphAxes, location):
    # https://docu.glyphsapp.com/#GSLayer.smartComponentPoleMapping
    smartComponentPoleMapping = {}
    for axis in glyphAxes:
        axisValue = location[axis.name]
        if axisValue != axis.minValue and axisValue != axis.maxValue:
            raise NotImplementedError(
                "Intermediate layers within smart glyphs are not yet implemented"
            )
        pole = (
            int(Pole.MIN)  # convert to int for Python <= 3.10
            if axis.minValue == axisValue
            else int(Pole.MAX)  # convert to int for Python <= 3.10
        )
        # Set pole, only MIN or MAX possible.
        # NOTE: In GlyphsApp these are checkboxes, either: on or off.
        smartComponentPoleMapping[axis.name] = pole
    return smartComponentPoleMapping


def fontraLayerToGSLayer(layer, gsLayer):
    gsLayer.paths = []

    # Draw new paths with pen
    pen = gsLayer.getPointPen()
    layer.glyph.path.drawPoints(pen)

    gsLayer.width = layer.glyph.xAdvance
    gsLayer.components = [
        fontraComponentToGSComponent(component) for component in layer.glyph.components
    ]
    gsLayer.anchors = [fontraAnchorToGSAnchor(anchor) for anchor in layer.glyph.anchors]
    gsLayer.guides = [
        fontraGuidelineToGSGuide(guideline) for guideline in layer.glyph.guidelines
    ]


EPSILON = 1e-9


def fontraComponentToGSComponent(component):
    if (
        abs(component.transformation.skewX) > EPSILON
        or abs(component.transformation.skewY) > EPSILON
    ):
        raise TypeError(
            "GlyphsApp Backend: Does not support skewing of components, yet."
        )
    gsComponent = glyphsLib.classes.GSComponent(component.name)
    transformation = component.transformation.toTransform()
    gsComponent.transform = GSTransform(*transformation)
    for axisName in component.location:
        gsComponent.smartComponentValues[axisName] = component.location[axisName]
    gsComponent.alignment = component.customData.get(
        "com.glyphsapp.component.alignment", 0
    )
    return gsComponent


def fontraAnchorToGSAnchor(anchor):
    gsAnchor = glyphsLib.classes.GSAnchor()
    gsAnchor.name = anchor.name
    gsAnchor.position.x = anchor.x
    gsAnchor.position.y = anchor.y
    if anchor.customData:
        gsAnchor.userData = anchor.customData
    # TODO: gsAnchor.orientation – If the position of the anchor
    # is relative to the LSB (0), center (2) or RSB (1).
    # Details: https://docu.glyphsapp.com/#GSAnchor.orientation
    return gsAnchor


def fontraGuidelineToGSGuide(guideline):
    gsGuide = glyphsLib.classes.GSGuide()
    gsGuide.name = guideline.name
    gsGuide.position.x = guideline.x
    gsGuide.position.y = guideline.y
    gsGuide.angle = guideline.angle
    gsGuide.locked = guideline.locked
    return gsGuide


def canParseFeatures(featureText, glyphNames):
    featureFile = io.StringIO(featureText)
    fea_parser = FeatureParser(
        featureFile,
        glyphNames=glyphNames,
        includeDir=None,
    )
    try:
        _ = fea_parser.parse()
    except FeatureLibError:
        return False
    return True


expandedFeaturesWarning = """\
#
# WARNING: proceed with caution!
#
# Fontra had to process the original feature code in either or both of these ways:
#
# 1. "include" statements have been expanded, and have been replaced by the *contents*
#    of the included file(s)
# 2. Glyphs-specific dynamic "token" syntax has been expanded to standard .fea syntax
#
# Editing the feature code in Fontra will write the *modified* code back to the .glyphs
# or .glyphspackage file, destroying the original features code.
#

"""


def expensiveGetFeatures(path):
    gsFont = glyphsLib.classes.GSFont(path)

    defaultMasterID = get_regular_master(gsFont).id
    try:
        featureText = glyphsLib.builder.features._to_ufo_features(
            gsFont,
            master=gsFont.masters[defaultMasterID],
            expand_includes=True,
        )
    except FeatureLibError:
        return None

    return expandedFeaturesWarning + featureText
