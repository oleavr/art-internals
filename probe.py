# Note: This tool requires Python >= 3.7

import collections
import glob
import os
import json
import re
import subprocess
import tempfile


root_dir = os.path.dirname(os.path.abspath(__file__))
aosp_dir = os.path.join(root_dir, "aosp")
cache_dir = os.path.join(root_dir, "cache")
art_repo_dir = os.path.join(aosp_dir, "platform", "art")
gtest_repo_dir = os.path.join(aosp_dir, "platform", "external", "gtest")

host_triplets_generic = {
    "x86": "i686-linux-android",
    "x86_64": "x86_64-linux-android",
    "arm": "arm-linux-androideabi",
    "arm64": "aarch64-linux-android",
}
host_triplets_abi = {
    "x86": "i686-linux-android",
    "x86_64": "x86_64-linux-android",
    "arm": "armv7a-linux-androideabi",
    "arm64": "aarch64-linux-android",
}
api_levels = {
    "5.0": 21,
    "5.1": 22,
    "6.0": 23,
    "7.0": 24,
    "7.1": 25,
    "8.0": 26,
    "8.1": 27,
    "9.0": 28,
    "10.0": 29,
}

tag_pattern = re.compile(r"^android-(((\d)\.(\d))|(q)-)")
section_data_pattern = re.compile(r"^\s+\d+\s+([\w\s]+)\s{2,}", re.MULTILINE)

ignored_errors = [
    "is not a member of",
    "has no member named",
]


def main():
    tags = compute_tags_affecting("runtime/mirror/art_field.h", "runtime/art_field.h")

    versions = [AndroidVersion.from_tag(tag) for tag in tags]

    result = collections.OrderedDict()
    for arch in ["arm", "x86", "arm64", "x86_64"]:
        for version in versions:
            size, access_flags = probe_offsets("runtime/mirror/art_field.h", "art::mirror::ArtField", ["access_flags_"], version, arch)
            if size < 0:
                size, access_flags = probe_offsets("runtime/art_field.h", "art::ArtField", ["access_flags_"], version, arch)

            key = "{}-{}".format(arch, version.api_level)
            value = "size={} access_flags={}".format(size, access_flags)

            print("// {} => {}".format(key, value))

            entries = result.get(key, [])
            entries.append(value)
            entries = list(set(entries))
            entries.sort()
            result[key] = entries

    print(json.dumps(result, indent=2))

def compute_tags_affecting(*paths):
    tags = []
    for path in paths:
        tags += compute_tags_affecting_path(path)
    return list(dict.fromkeys(tags).keys())

def compute_tags_affecting_path(path):
    result = []

    tags = filter(is_relevant_tag, run_in_art_repo("git", "tag", "--sort=committerdate").split("\n"))

    previous_tag = None
    for i, tag in enumerate(tags):
        if i == 0:
            result.append(tag)
        else:
            diff = run_in_art_repo("git", "diff", previous_tag, tag, "--", path)
            if len(diff) > 0:
                result.append(tag)

        previous_tag = tag

    return result

def probe_offsets(header, class_name, field_names, version, arch):
    system_core_dir = get_aosp_checkout(["platform", "system", "core"], version)
    art_dir = get_aosp_checkout(["platform", "art"], version)

    header_path = os.path.join(art_dir, header)
    if not os.path.exists(header_path):
        return [-1] + [-1 for n in field_names]

    with tempfile.NamedTemporaryFile(prefix="probe", suffix=".cc", mode="w", encoding="utf-8") as probe_source:
        includes = [
            "#include <cstring>",
            "#include <runtime/runtime.h>",
            "#include <{}>".format(header)
        ]

        queries = ["sizeof ({})".format(class_name)]
        queries += ["offsetof ({}, {})".format(class_name, field_name) for field_name in field_names]

        probe_source.write("""\
#include <cstdlib>

{includes}

unsigned int values[] =
{{
  {queries}
}};
""".format(includes="\n".join(includes), queries=",\n  ".join(queries)))
        probe_source.flush()

        with open(header_path, "r", encoding="utf-8") as f:
            header_source = f.read()
        header_source = header_source.replace("protected:", "public:").replace("private:", "public:")
        with open(header_path, "w", encoding="utf-8") as f:
            f.write(header_source)

        if version.major >= 7:
            toolchain = get_toolchain(arch, "clang")
            system_core_includes = [
                "-I", os.path.join(system_core_dir, "include"),
                "-I", os.path.join(system_core_dir, "base", "include"),
            ]
        else:
            toolchain = get_toolchain(arch, "gcc")
            system_core_includes = [
            ]

        probe_obj = os.path.splitext(probe_source.name)[0] + ".o"
        try:
            result = subprocess.run([
                toolchain.cxx
                ] + toolchain.cxxflags + [
                "-DANDROID_SMP=1",
                "-DIMT_SIZE=64",
                "-DART_STACK_OVERFLOW_GAP_arm=8192",
                "-DART_STACK_OVERFLOW_GAP_arm64=8192",
                "-DART_STACK_OVERFLOW_GAP_mips=16384",
                "-DART_STACK_OVERFLOW_GAP_mips64=16384",
                "-DART_STACK_OVERFLOW_GAP_x86=8192",
                "-DART_STACK_OVERFLOW_GAP_x86_64=8192",
                "-Wno-invalid-offsetof",
                ] + system_core_includes + [
                "-I", os.path.join(gtest_repo_dir, "include"),
                "-I", os.path.join(art_dir, "libartbase"),
                "-I", os.path.join(art_dir, "libdexfile"),
                "-I", os.path.join(art_dir, "runtime"),
                "-I", art_dir,
                probe_source.name,
                "-c",
                "-o", probe_obj,
            ], capture_output=True, encoding="utf-8")
            if result.returncode != 0:
                for e in ignored_errors:
                    if e in result.stderr:
                        return [-2] + [-2 for n in field_names]
                print(result.stderr)
            result.check_returncode()

            return parse_objdump_section_as_uint32_array(subprocess.run([
                toolchain.objdump,
                "-sj", ".data",
                probe_obj
            ], check=True, capture_output=True, encoding="utf-8").stdout)
        finally:
            if os.path.exists(probe_obj):
                os.unlink(probe_obj)

def is_relevant_tag(name):
    version = try_parse_tag(name)
    if version is None:
        return False
    major = version[0]
    return major >= 5

def try_parse_tag(name):
    m = tag_pattern.match(name)
    if m is None:
        return None

    major = try_parse_version_component(m.group(3))
    minor = try_parse_version_component(m.group(4))
    codename = m.group(5)

    if major is None:
        assert codename == "q"
        major = 10
        minor = 0

    return (major, minor)

def try_parse_version_component(component):
    if component is None:
        return None
    return int(component)

def run_in_art_repo(*args):
    try:
        return subprocess.run(args, cwd=art_repo_dir, capture_output=True, encoding="utf-8", check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(e.stderr)
        raise

def get_aosp_checkout(repo, version):
    tag = version.tag
    worktree_dir = os.path.join(cache_dir, tag, *repo)
    if not os.path.isdir(worktree_dir):
        repo_dir = os.path.join(aosp_dir, *repo)
        subprocess.run(["git", "worktree", "add", worktree_dir, tag], cwd=repo_dir, capture_output=True, check=True)
    return worktree_dir

def get_toolchain(arch, flavor):
    host_triplet_generic = host_triplets_generic[arch]
    api_level = 21 if "64" in arch else 16

    if flavor == "clang":
        host_triplet_abi = host_triplets_abi[arch] + str(api_level)

        install_dir = glob.glob(os.path.join(os.environ["ANDROID_NDK_R21_ROOT"], "toolchains", "llvm", "prebuilt", "*"))[0]
    else:
        host_triplet_abi = host_triplet_generic

        install_dir = os.path.join(cache_dir, "toolchains", "-".join([arch, flavor]))
        if not os.path.isdir(install_dir):

            subprocess.run([
                os.path.join(os.environ["ANDROID_NDK_R17B_ROOT"], "build", "tools", "make_standalone_toolchain.py"),
                "--arch", arch,
                "--api", str(api_level),
                "--stl", "gnustl",
                "--install-dir", install_dir
            ], capture_output=True, check=True)

    bin_dir = os.path.join(install_dir, "bin")

    cxx_name = "clang++" if flavor == "clang" else "g++"
    cxx = os.path.join(bin_dir, "-".join([host_triplet_abi, cxx_name]))
    cxxflags = []
    objdump = os.path.join(bin_dir, "-".join([host_triplet_generic, "objdump"]))

    if flavor == "clang":
        cxxflags = [
            "-std=c++2a",
        ]
    else:
        cxxflags = [
            "-std=c++14",
        ]
    if arch == "arm":
        cxxflags += [
            "-march=armv7-a",
            "-mthumb",
        ]
    if flavor == "clang":
        cxxflags += [
            "-Wno-inconsistent-missing-override",
        ]

    return Toolchain(cxx, cxxflags, objdump)

def parse_objdump_section_as_uint32_array(output):
    hex_bytes = "".join(section_data_pattern.findall(output)).replace(" ", "")
    raw_bytes = [int(hex_bytes[i:i + 2], 16) for i in range(0, len(hex_bytes), 2)]
    return [int.from_bytes(raw_bytes[i:i + 4], byteorder="little") for i in range(0, len(raw_bytes), 4)]


class AndroidVersion(object):
    def __init__(self, tag, major, minor, api_level):
        self.tag = tag
        self.major = major
        self.minor = minor
        self.api_level = api_level

    @staticmethod
    def from_tag(tag):
        major, minor = try_parse_tag(tag)

        api_level = api_levels["{}.{}".format(major, minor)]

        return AndroidVersion(tag, major, minor, api_level)


class Toolchain(object):
    def __init__(self, cxx, cxxflags, objdump):
        self.cxx = cxx
        self.cxxflags = cxxflags
        self.objdump = objdump


main()
