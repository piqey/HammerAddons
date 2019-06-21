"""Implementation of CSGO's propcombine feature.

This merges static props together, so they can be drawn with a single
draw call.
"""
import os
import subprocess
import random
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Dict, List, Set, Optional, Tuple, Callable, FrozenSet,
    Iterable, Iterator,
    IO,
)

from srctools import (
    Vec, VMF, Entity, conv_int, Property, AtomicWriter,
    bool_as_int,
)
from srctools.tokenizer import Tokenizer, Token

from srctools.game import Game

from srctools.logger import get_logger
from srctools.packlist import PackList
from srctools.bsp import BSP, StaticProp, StaticPropFlags
from srctools.mdl import Model
from srctools.smd import Mesh
from collections import defaultdict, namedtuple


LOGGER = get_logger(__name__)

QC = namedtuple('QC', [
    'path',  # QC path.
    'ref_smd',    # Location of main visible geometry.
    'phy_smd',    # Relative location of collision model, or None
    'ref_scale',  # Scale of main model.
    'phy_scale',  # Scale of collision model.
])

QC_TEMPLATE = '''\
$staticprop
$modelname "{path}.mdl"
$surfaceprop "{surf}"

$body body "{ref_mesh}"

$contents {contents}

$sequence idle anim act_idle 1
'''

QC_COLL_TEMPLATE = '''
$collisionmodel "{}" {{
    $maxconvexpieces 2048
    $automass
    $concave
}}
'''


MDL_EXTS = [
    '.mdl',
    '.phy',
    '.dx90.vtx',
    '.dx80.vtx',
    '.sw.vtx',
    '.vvd',
]

MAX_GROUP = 24  # Studiomdl does't allow more than this...


class DynamicModel(Exception):
    """Used as flow control."""


def unify_mdl(path: str):
    """Compute a 'canonical' path for a given model."""
    path = path.casefold().replace('\\', '/')
    if not path.startswith('models/'):
        path = 'models/' + path
    if not path.endswith('.mdl'):
        path = path + '.mdl'
    return path


def in_bbox(pos: Vec, bb_min: Vec, bb_max: Vec) -> bool:
    """Check if the given position is inside a bounding box."""
    if pos.x < bb_min.x or bb_max.x < pos.x:
        return False
    if pos.y < bb_min.y or bb_max.y < pos.y:
        return False
    if pos.z < bb_min.z or bb_max.z < pos.z:
        return False
    return True

# 'key' used to match models to each other.
PropPos = namedtuple('PropPos', [
    'x', 'y', 'z',
    'pit', 'yaw', 'rol',
    'model', 'skin',
])


class MergedModel:
    """Represents a model that has been merged together."""
    def __init__(self, name: str, has_coll: bool) -> None:
        self.name = name
        self.has_coll = has_coll
        self.used = False


class ModelManager:
    """Manages the set of merged models that have been generated."""

    def __init__(
        self,
        temp_folder: Path,
        game: Game,
        studiomdl_loc: Path,
        pack: PackList,
        qc_map: Dict[str, QC],
        map_name: str,
        get_model: Callable[[str], Model],
    ) -> None:
        # The models already constructed.
        self._mdl_cache = {}  # type: Dict[FrozenSet[PropPos], MergedModel]

        # Cache of the SMD models we have already parsed, so we don't need
        # to parse them again. The second is the collision model.
        self._mesh_cache = {}  # type: Dict[Tuple[QC, int], Tuple[Mesh, Optional[Mesh]]]

        # The random indexes we use to produce filenames.
        self._mdl_names = set()  # type: Set[str]

        # Function to load and parse a MDL.
        self.lookup_model = get_model
        # The QCs we have parsed.
        self.qc_map = qc_map

        # The stuff we need to know to build models.
        self.game = game
        self.studiomdl_loc = str(studiomdl_loc.resolve())
        self.model_folder = 'maps/{}/propcombine/'.format(map_name)
        self.temp_folder = temp_folder
        self.pack = pack

    def load(self) -> None:
        """Load from the cache file."""
        with open(str(self.game.path / 'models' / self.model_folder / 'cache.vdf')) as f:
            prop_block = Property.parse(f)
        for prop in prop_block:
            pos_set = set()
            for pos_block in prop.find_all('model'):
                origin = pos_block.vec('origin')
                angles = pos_block.vec('angles')
                pos_set.add(PropPos(
                    origin.x, origin.y, origin.z,
                    angles.x, angles.y, angles.z,
                    pos_block['filename'],
                    pos_block.int('skin'),
                ))
            mdl_name = prop['name']
            self._mdl_names.add(mdl_name)
            self._mdl_cache[frozenset(pos_set)] = MergedModel(
                mdl_name,
                prop.bool('has_coll'),
            )
        LOGGER.info('Found {} existing grouped models.', len(self._mdl_cache))

    def finalise(self) -> None:
        """Write the constructed models to the cache file and remove unused models."""
        used_models = set()

        with AtomicWriter(
            str(self.game.path / 'models' / self.model_folder / 'cache.vdf'),
        ) as f:
            for positions, mdl in self._mdl_cache.items():
                if not mdl.used:
                    continue

                used_models.add(mdl.name)

                prop = Property('PropGroup', [
                    Property('name', mdl.name),
                    Property('has_coll', bool_as_int(mdl.has_coll)),
                ])
                for pos in positions:
                    prop.append(Property('Model', [
                        Property('filename', pos.model),
                        Property('skin', str(pos.skin)),
                        Property(
                            'origin',
                            '{:g} {:g} {:g}'.format(pos.x, pos.y, pos.z),
                        ),
                        Property(
                            'angles',
                            '{:g} {:g} {:g}'.format(pos.pit, pos.yaw, pos.rol)
                        ),
                    ]))
                for line in prop.export():
                    f.write(line)

        for mdl_file in (self.game.path / 'models' / self.model_folder).glob('*'):
            if mdl_file.suffix not in {'.mdl', '.phy', 'vtx', '.vvd'}:
                continue

            if mdl_file.stem in used_models:
                continue

            LOGGER.info('Culling {}...', mdl_file)
            try:
                mdl_file.unlink()
            except FileNotFoundError:
                pass

    def combine_group(self, props: List[StaticProp]) -> StaticProp:
        """Merge the given props together, compiling a model if required."""

        # We want to allow multiple props to reuse the same model.
        # To do this try and match prop groups to each other, by "unifying"
        # them into a consistent orientation.
        #
        # If there are matches in different orientations, they're most likely
        # 90 degree or other rotations in the yaw axis. So we compute the average,
        # and subtract that out.

        avg_pos = Vec()
        avg_yaw = 0.0

        visleafs = set()  # type: Set[int]

        for prop in props:
            avg_pos += prop.origin
            avg_yaw += prop.angles.y
            visleafs.update(prop.visleafs)

        avg_pos /= len(props)
        avg_yaw = 0

        prop_pos = set()
        for prop in props:
            origin = round((prop.origin - avg_pos).rotate(0, -avg_yaw, 0), 7)
            angles = round(Vec(prop.angles), 7)
            prop_pos.add(PropPos(
                origin.x, origin.y, origin.z,
                angles.x, angles.y, angles.z,
                prop.model,
                prop.skin,
            ))
        prop_key = frozenset(prop_pos)

        try:
            merged = self._mdl_cache[prop_key]
        except KeyError:
            # Need to build the model.
            # We don't need to make a collision mesh if the prop is set to
            # not use them.
            # All the props are the same as the first.
            has_coll = props[0].solidity != 0

            # Figure out a name to use.
            while True:
                mdl_name = 'merge_{:04x}'.format(random.getrandbits(16))
                if mdl_name not in self._mdl_names:
                    self._mdl_names.add(mdl_name)
                    break

            merged = self._mdl_cache[prop_key] = MergedModel(mdl_name, has_coll)
            self.compile(prop_key, merged)

        if not merged.used:
            # Pack it in.
            merged.used = True

            full_model_path = Path(
                self.game.path,
                'models',
                self.model_folder,
                merged.name
            )
            for ext in MDL_EXTS:
                try:
                    with open(str(full_model_path.with_suffix(ext)), 'rb') as fb:
                        self.pack.pack_file(
                            'models/{}{}{}'.format(
                                self.model_folder, merged.name, ext,
                            ),
                            data=fb.read(),
                        )
                except FileNotFoundError:
                    pass

            if not merged.has_coll:
                # Make sure an older collision mesh isn't left behind!
                try:
                    os.remove(full_model_path.with_suffix('.phy'))
                except FileNotFoundError:
                    pass

        # Many of these we require to be the same, so we can read them
        # from any of the component props.
        return StaticProp(
            model='models/{}{}.mdl'.format(self.model_folder, merged.name),
            origin=avg_pos,
            angles=Vec(0, 270, 0),
            scaling=1.0,
            visleafs=sorted(visleafs),
            solidity=0 if not merged.has_coll else props[0].solidity,
            flags=props[0].flags,
            lighting_origin=avg_pos,
            tint=props[0].tint,
            renderfx=props[0].renderfx,
        )

    def compile(
        self,
        prop_pos: FrozenSet[PropPos],
        merged: MergedModel,
    ) -> None:
        """Build this merged model."""
        LOGGER.info('Compiling {}{}.mdl...', self.model_folder, merged.name)

        # Unify these properties.
        surfprops = set()  # type: Set[str]
        cdmats = set()  # type: Set[str]
        contents = set()  # type: Set[int]

        for prop in prop_pos:
            mdl = self.lookup_model(prop.model)
            surfprops.add(mdl.surfaceprop.casefold())
            cdmats.update(mdl.cdmaterials)
            contents.add(mdl.contents)

        if len(surfprops) > 1:
            raise ValueError('Multiple surfaceprops? Should be filtered out.')

        if len(contents) > 1:
            raise ValueError('Multiple contents? Should be filtered out.')

        [surfprop] = surfprops
        [phy_content_type] = contents

        ref_mesh = Mesh.blank('static_prop')
        coll_mesh = None  #  type: Optional[Mesh]

        for prop in prop_pos:
            qc = self.qc_map[unify_mdl(prop.model)]
            mdl = self.lookup_model(prop.model)

            try:
                child_ref, child_coll = self._mesh_cache[qc, prop.skin]
            except KeyError:
                LOGGER.info('Parsing ref "{}"', qc.ref_smd)
                with open(qc.ref_smd, 'rb') as fb:
                    child_ref = Mesh.parse_smd(fb)
                if qc.phy_smd is not None:
                    LOGGER.info('Parsing coll "{}"', qc.phy_smd)
                    with open(qc.phy_smd, 'rb') as fb:
                        child_coll = Mesh.parse_smd(fb)
                else:
                    child_coll = None

                if prop.skin != 0 and prop.skin <= len(mdl.skins):
                    # We need to rename the materials to match the skin.
                    swap_skins = dict(zip(
                        mdl.skins[0],
                        mdl.skins[prop.skin]
                    ))
                    for tri in child_ref.triangles:
                        tri.mat = swap_skins.get(tri.mat, tri.mat)

                # For some reason all the SMDs are rotated badly, but only
                # if we append them.
                for tri in child_ref.triangles:
                    for vert in tri:
                        vert.pos.rotate(0, 90, 0, round_vals=False)
                        vert.norm.rotate(0, 90, 0, round_vals=False)
                if child_coll is not None:
                    for tri in child_coll.triangles:
                        for vert in tri:
                            vert.pos.rotate(0, 90, 0, round_vals=False)
                            vert.norm.rotate(0, 90, 0, round_vals=False)

                self._mesh_cache[qc, prop.skin] = child_ref, child_coll

            offset = Vec(prop.x, prop.y, prop.z)
            angles = Vec(prop.pit, prop.yaw, prop.rol)

            ref_mesh.append_model(child_ref, angles, offset)

            if merged.has_coll and child_coll is not None:
                if coll_mesh is None:
                    coll_mesh = Mesh.blank('static_prop')
                coll_mesh.append_model(child_coll, angles, offset)

        with open(str(self.temp_folder / (merged.name + '_ref.smd')), 'wb') as fb:
            ref_mesh.export(fb)

        if coll_mesh is not None:
            with open(str(self.temp_folder / (merged.name + '_phy.smd')), 'wb') as fb:
                coll_mesh.export(fb)

        with open(str((self.temp_folder / merged.name).with_suffix('.qc')), 'w') as f:
            f.write(QC_TEMPLATE.format(
                path=self.model_folder + merged.name,
                surf=surfprop,
                ref_mesh=merged.name + '_ref.smd',
                # For $contents, we need to decompose out each bit.
                # This is the same as BSP's flags in public/bsp_flags.h
                # However only a few types are allowable.
                contents=' '.join([
                    cont
                    for mask, cont in [
                        (0x1, '"solid"'),
                        (0x8, '"grate"'),
                        (0x2000000, '"monster"'),
                        (0x20000000, '"ladder"'),
                    ]
                    if mask & phy_content_type
                    # 0 needs to produce this value.
                ]) or '"notsolid"',
            ))

            for mat in sorted(cdmats):
                f.write('$cdmaterials "{}"\n'.format(mat))

            if coll_mesh is not None:
                f.write(QC_COLL_TEMPLATE.format(merged.name + '_phy.smd'))

        args = [
            self.studiomdl_loc,
            '-nop4',
            '-game', str(self.game.path),
            str((self.temp_folder / merged.name).with_suffix('.qc')),
        ]
        subprocess.run(args, stdout=subprocess.DEVNULL)


def load_qcs(qc_folder: Path) -> Dict[str, QC]:
    """Parse through all the QC files to match to compiled models."""
    qc_map = {}

    for qc_path in qc_folder.rglob('*.qc'):  # type: Path
        model_name = ref_smd = phy_smd = None
        scale_factor = ref_scale = phy_scale = 1.0
        qc_loc = qc_path.parent
        try:
            with open(qc_path) as f:
                tok = Tokenizer(f, qc_path, allow_escapes=False)
                for token_type, token_value in tok:

                    if model_name and ref_smd and phy_smd:
                        break

                    if token_type is Token.STRING:
                        token_value = token_value.casefold()
                        if token_value == '$scale':
                            scale_factor = float(tok.expect(Token.STRING))
                        elif token_value == '$modelname':
                            model_name = tok.expect(Token.STRING)
                        elif token_value in ('$bodygroup', '$body', '$model'):
                            tok.expect(Token.STRING)  # group name.
                            body_type, body_value = tok()
                            if body_type is Token.STRING:
                                # $body name "file.smd"
                                if ref_smd:
                                    raise DynamicModel
                                else:
                                    ref_smd = qc_loc / body_value
                                    ref_scale = scale_factor
                                continue
                            elif body_type is Token.NEWLINE:
                                tok.expect(Token.BRACE_OPEN)
                            elif body_type is not Token.BRACE_OPEN:
                                raise tok.error(body_type)

                            for body_type, body_value in tok:
                                if body_type is Token.BRACE_CLOSE:
                                    break
                                elif body_type is Token.STRING:
                                    if body_value.casefold() == "studio":
                                        if ref_smd:
                                            raise DynamicModel
                                        else:
                                            ref_smd = qc_loc / tok.expect(Token.STRING)
                                            ref_scale = scale_factor
                                elif body_type is not Token.NEWLINE:
                                    raise tok.error(body_type)

                        elif token_value == '$collisionmodel':
                            phy_smd = qc_loc / tok.expect(Token.STRING)
                            phy_scale = scale_factor

                        # We can't support this.
                        elif token_value in (
                            '$collisionjoints',
                            '$ikchain',
                            '$weightlist',
                            '$poseparameter',
                            '$proceduralbones',
                            '$jigglebone',
                            '$keyvalues',
                            # Allow LOD models, propcombine is better than that.
                            # '$lod',
                        ):
                            raise DynamicModel

        except DynamicModel:
            # It's a dynamic QC, we can't combine.
            continue
        if model_name is None or ref_smd is None:
            # Malformed...
            LOGGER.warning('Cannot parse "{}"... ({}, {})', qc_path, model_name, ref_smd)
            continue

        # We can't parse FBX files right now.
        if ref_smd.suffix.casefold() not in ('.smd', '.dmx'):
            LOGGER.warning('Reference mesh not a SMD/DMX:\n{}', ref_smd)
            continue

        if phy_smd is not None:
            if phy_smd.suffix.casefold() not in ('.smd', '.dmx'):
                LOGGER.warning('Collision mesh not a SMD/DMX:\n{}', ref_smd)
                continue

        qc_map[unify_mdl(model_name)] = QC(
            str(qc_path).replace('\\', '/'),
            str(ref_smd).replace('\\', '/'),
            str(phy_smd).replace('\\', '/') if phy_smd else None,
            ref_scale,
            phy_scale,
        )
    return qc_map


def group_props_ent(
    prop_groups: Dict[tuple, List[StaticProp]],
    rejected: List[StaticProp],
    get_model: Callable[[str], Optional[Model]],
    bbox_ents: List[Entity],
    min_cluster: int,
) -> Iterator[List[StaticProp]]:
    """Given the groups of props, merge props according to the provided ents."""
    bbox_groups = defaultdict(list)  # type: Dict[Tuple[str, FrozenSet[str]], List[Tuple[Vec, Vec]]]

    empty_fs = frozenset('')

    for ent in bbox_ents:
        # Either provided name, or unique value.
        name = ent['name'] or format(int(ent['hammerid']), 'X')
        origin = Vec.from_str(ent['origin'])

        skinset = empty_fs

        mdl_name = ent['prop']
        if mdl_name:
            mdl = get_model(mdl_name)
            if mdl is not None:
                skinset = frozenset(mdl.iter_textures([conv_int(ent['skin'])]))

        bbox_groups[name, skinset].append(Vec.bbox(
            Vec.from_str(ent['mins']) + origin,
            Vec.from_str(ent['maxs']) + origin,
        ))

    # Each of these groups cannot be merged with other ones.
    for group_key, group in prop_groups.items():
        # No point merging single/empty groups.
        group_skinset = group_key[0]
        if len(group) < min_cluster:
            rejected.extend(group)
            group.clear()
            continue

        for (name, skinset), bboxes in bbox_groups.items():
            if skinset and skinset != group_skinset:
                continue  # No match
            found = defaultdict(list)  # type: Dict[int, List[StaticProp]]
            for prop in list(group):
                for bbox_min, bbox_max in bboxes:
                    if in_bbox(prop.origin, bbox_min, bbox_max):
                        # Group by this bbox list object's identity.
                        found[id(bboxes)].append(prop)
                        group.remove(prop)
                        break

            for group in found.values():
                if len(group) < min_cluster:
                    rejected.extend(group)
                else:
                    yield group

    # Finally, reject all the ones not in a bbox.
    for group in prop_groups.values():
        rejected.extend(group)


def group_props_auto(
    prop_groups: Dict[object, List[StaticProp]],
    rejected: List[StaticProp],
    dist: float,
    min_cluster: int,
) -> Iterator[List[StaticProp]]:
    """Given the groups of props, automatically find close props to merge."""
    # Each of these groups cannot be merged with other ones.

    dist_sq = dist * dist
    large_dist_sq = 4 * dist_sq

    for group in prop_groups.values():
        # No point merging single/empty groups.
        if len(group) < 2:
            rejected.extend(group)
            continue

        todo = set(group)
        while todo:
            center = todo.pop()
            cluster = {center}

            for prop in todo:
                if (center.origin - prop.origin).mag_sq() <= large_dist_sq:
                    cluster.add(prop)
                    if len(cluster) > MAX_GROUP:
                        # Limit the number of maximum props that can be used.
                        break

            if len(cluster) < min_cluster:
                rejected.append(center)
                continue

            bbox_min, bbox_max = Vec.bbox(prop.origin for prop in cluster)
            center_pos = (bbox_min + bbox_max) / 2

            cluster_list = []

            for prop in cluster:
                prop_off = (center_pos - prop.origin).mag_sq()
                if prop_off <= dist_sq:
                    cluster_list.append((prop, prop_off))

            cluster_list.sort(key=lambda t: t[1])
            cluster_list = [
                prop for prop, off in
                cluster_list[:MAX_GROUP]
            ]
            todo.difference_update(cluster_list)

            if len(cluster_list) >= min_cluster:
                yield cluster_list
            else:
                rejected.extend(cluster_list)


def combine(
    bsp: BSP,
    bsp_ents: VMF,
    pack: PackList,
    game: Game,
    studiomdl_loc: Path=None,
    qc_folder: Path=None,
    auto_range: float=0,
    min_cluster: int=2,
) -> None:
    """Combine props in this map."""

    # First parse out the bbox ents, so they are always removed.
    bbox_ents = list(bsp_ents.by_class['comp_propcombine_set'])
    for ent in bbox_ents:
        ent.remove()

    if studiomdl_loc is None:
        studiomdl_loc = game.bin_folder() / 'studiomdl.exe'

    if not studiomdl_loc.exists():
        LOGGER.warning('No studioMDL! Cannot propcombine!')
        return

    if qc_folder is None:
        # If gameinfo is blah/game/hl2/gameinfo.txt,
        # QCs should be in blah/content/ according to Valve's scheme.
        # But allow users to override this.
        qc_folder = game.path.parent.parent / 'content'

    # Parse through all the QC files.
    LOGGER.info('Parsing QC files. Path: {}', qc_folder)
    qc_map = load_qcs(qc_folder)
    LOGGER.info('Done! {} props.', len(qc_map))

    map_name = Path(bsp.filename).stem

    # Don't re-parse models continually.
    mdl_map = {}  # type: Dict[str, Model]

    def get_model(filename: str) -> Optional[Model]:
        key = unify_mdl(filename)
        try:
            model = mdl_map[key]
        except KeyError:
            try:
                mdl_file = pack.fsys[filename]
            except FileNotFoundError:
                # We don't have this model, we can't combine...
                return None
            model = mdl_map[key] = Model(pack.fsys, mdl_file)
        return model

    def get_grouping_key(prop: StaticProp) -> object:
        """Compute a grouping key for this prop.

        Only props with matching key can be possibly combined.
        If None it cannot be combined.
        """
        # If a lighting origin was specified, that overrides the default.
        if prop.flags & StaticPropFlags.HAS_LIGHTING_ORIGIN:
            return None

        key = unify_mdl(prop.model)

        if key not in qc_map:
            # We don't have source for this...
            return None

        model = get_model(prop.model)

        if model is None:
            return None

        return (
            # Must be first!
            frozenset(model.iter_textures([prop.skin])),
            model.flags.value,
            prop.flags.value,
            model.contents,
            model.surfaceprop,
            prop.solidity,
            prop.renderfx,
            *prop.tint,
        )

    prop_count = 0

    # First, construct groups of props that can possibly be combined.
    prop_groups = defaultdict(list)  # type: Dict[tuple, List[StaticProp]]

    # This holds the list of all props we want in the map -
    # combined ones, and any we reject for whatever reason.
    final_props = []  # type: List[StaticProp]

    if bbox_ents:
        LOGGER.info('Propcombine sets present, combining...')
        grouper = group_props_ent(
            prop_groups, final_props,
            get_model,
            bbox_ents,
            min_cluster,
        )
    elif auto_range > 0:
        LOGGER.info('Automatically finding propcombine sets...')
        grouper = group_props_auto(
            prop_groups, final_props,
            auto_range,
            min_cluster,
        )
    else:
        # No way provided to choose props.
        LOGGER.info('No propcombine groups provided.')
        return

    for prop in bsp.static_props():
        prop_groups[get_grouping_key(prop)].append(prop)
        prop_count += 1

    # These are models we cannot merge no matter what -
    # no source files etc.
    final_props.extend(prop_groups.pop(None, []))

    with TemporaryDirectory(prefix='autocomb_') as temp_dir:
        # Generate a blank animation.
        with open(temp_dir + '/anim.smd', 'wb') as f:
            Mesh.blank('static_prop').export(f)

        mdl_man = ModelManager(
            Path(temp_dir),
            game,
            studiomdl_loc,
            pack,
            qc_map,
            map_name,
            get_model,
        )

        try:
            mdl_man.load()
        except FileNotFoundError:
            pass  # Make a new one.
        except IOError:
            LOGGER.warning("Couldn't parse props cache file:", exc_info=True)

        for group in grouper:
            final_props.append(mdl_man.combine_group(group))

    mdl_man.finalise()

    LOGGER.info('Combined {} props to {} props', prop_count, len(final_props))

    bsp.write_static_props(final_props)
