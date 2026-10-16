# SPDX-License-Identifier: GPL-3.0-or-later
"""Extension -> content class, and the junk/archive sets the findings rely on."""

CLASSES = {
    "video":    "mp4 mkv mov avi webm wmv flv mpg mpeg m4v ts vob ogv",
    "image":    "jpg jpeg png gif bmp tga tiff tif webp svg psd xcf dds heic ico exr hdr",
    "audio":    "mp3 wav flac ogg aac m4a wma aiff mid midi",
    "archive":  "zip rar 7z tar gz xz bz2 iso dmg pkg cab msi tgz zst lz4",
    "code":     ("c cpp cc cxx h hpp hh cs py js ts jsx tsx java kt swift m mm rs go rb php "
                 "lua sh bat ps1 sql dart vue scss css html sln vcxproj csproj cmake gradle pro pri"),
    "doc":      "pdf doc docx xls xlsx ppt pptx txt md rtf odt ods csv epub pages numbers key",
    "model3d":  "fbx obj glb gltf dae blend stl 3ds max ma mb usd usda abc ply unitypackage uasset umap",
    "gamedata": ("rpf img txd dff ide ipl dat col wtd wft ydr ydd yft ytd ybn ynv cdimage big arc bsa "
                 "pak nefs rpkg rda vpk ucas utoc sds vfs0 ttarch2 ba2 tiger bk2 bik pck psb pc "
                 "xxx forge sarc bundle vol wad grp gcf vpp cpk afs nxg toc sga asset resource "
                 "cache bsp gma dcx upk pss usm wem fsb bnk"),
    "ml_model": "safetensors ckpt pt pth onnx gguf h5 pb tflite",
    "binary":   "exe dll so dylib a lib o obj pdb bin elf app sys kext",
    "junk":     "dmp log tmp temp crash ips bak old part crdownload",
    "db":       "db sqlite sqlite3 mdb dump bson",
}
EXT2CLASS = {e: c for c, exts in CLASSES.items() for e in exts.split()}

# archives whose extracted folder may sit beside them
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "iso"}
# safe to delete on sight, above a size floor
JUNK_EXTS = {"dmp", "tmp", "cache", "crash", "crdownload", "part"}

# directories recorded as one aggregate row instead of being walked
BLOB_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "Pods", ".gradle", ".venv", "venv",
    "__pycache__", ".next", ".nuxt", "target", ".cargo", ".rustup", ".npm",
    ".pub-cache", ".cocoapods", ".conda", "site-packages", ".terraform",
    "DerivedData", ".tox", "bower_components", ".pnpm-store", ".yarn",
}
# skipped entirely: OS bookkeeping with no organizational value
SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", "$Recycle.Bin", "$WinREAgent",
    "$GetCurrent", "$SysReset", "$AV_AVG", "$Windows.~WS", "Windows", "WinSxS",
    "Config.Msi", "MATS", "PerfLogs", "OneDriveTemp", "Recovery",
    ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".Trashes",
    ".DocumentRevisions-V100", ".fcpcache",
}


def classify(ext):
    if not ext:
        return "sem_ext"
    return EXT2CLASS.get(ext, "outros")
