import os
import os.path as op
import platform
import shutil
import stat
import sys
import zipfile
from datetime import datetime
from urllib import error, parse, request

# ----------------------------------------------------------------------
#   EXCEPTIONS
# ----------------------------------------------------------------------


class VersionNotFoundError(RuntimeError):
    ...


class DownloadError(RuntimeError):
    ...


class UnsupportedArchError(RuntimeError):
    ...


class UserInterruptionError(RuntimeError):
    ...


# ----------------------------------------------------------------------
#   USER INPUT
# ----------------------------------------------------------------------


def askuser(question, default="yes", auto_answer=False, raise_if_no=False):
    options = "([yes]/no)" if default == "yes" else "(yes/[no])"
    if auto_answer:
        yesno = True if default == "yes" else False
    else:
        yesno = input(f"{question} {options}").strip()
        yesno = (not yesno) if default == "yes" else False
        yesno = yesno or yesno[:1].lower() == "y"
    if not yesno and raise_if_no:
        raise UserInterruptionError(question)
    return yesno


# ----------------------------------------------------------------------
#   UNZIP WITH EXEC PERMISSION + SYMLINKS
# ----------------------------------------------------------------------

# Running the matlab installer fails if I naively unzip using ZipFile.
# That's because (unlike the `unzip` tool on unix), ZipFile does not
# preserve executable permissions, and does not preserve symlinks.
# This somehow breaks the linkking of the dylibs (on mac -- but probably
# also on linux). This patched ZipFile fixes these two issues.


class ZipFileWithExecPerm(zipfile.ZipFile):
    # https://stackoverflow.com/questions/39296101

    def _extract_member(self, member, targetpath, pwd):
        if not isinstance(member, zipfile.ZipInfo):
            member = self.getinfo(member)

        targetpath = super()._extract_member(member, targetpath, pwd)

        if stat.S_ISLNK(member.external_attr >> 16) and \
                hasattr(os, "symlink"):     # Symlink
            link = self.open(member, pwd=pwd).read()
            try:
                os.symlink(link, targetpath)
                return targetpath
            except OSError:     # No permission to create symlink
                pass

        attr = member.external_attr >> 16

        # https://bugs.python.org/issue27318
        if stat.S_ISLNK(attr) and hasattr(os, "symlink"):
            link = self.open(member, pwd=pwd).read()
            shutil.move(targetpath, targetpath + ".__backup__")
            try:
                os.symlink(link, targetpath)
                return targetpath
            except OSError:     # No permission to create symlink
                shutil.move(targetpath + ".__backup__", targetpath)
                pass

        if attr != 0:
            os.chmod(targetpath, attr)
        return targetpath


# ----------------------------------------------------------------------
#   URL REQUESTS
# ----------------------------------------------------------------------


class NoRedirection(request.HTTPErrorProcessor):
    # https://stackoverflow.com/questions/29327674
    def http_response(self, request, response):
        return response
    https_response = http_response


def url_exists(url):
    opener = request.build_opener(NoRedirection)
    req = request.Request(url, method="HEAD")
    try:
        with opener.open(req) as res:
            status = res.status
        return status < 400
    except error.HTTPError:
        return False


def url_download(url, out):
    if op.isdir(out):
        basename = op.basename(parse.urlparse(url).path)
        out = op.join(out, basename)
    req = request.Request(url, method="GET")
    with request.urlopen(req) as res:
        if res.status >= 400:
            raise DownloadError(f"[{res.status}] Failed to download", url)
        with open(out, "wb") as f:
            f.write(res.read())
    return out


# ----------------------------------------------------------------------
#   SYSTEM/ARCH
# ----------------------------------------------------------------------


def guess_arch():

    try:
        arch = {
            "Darwin": "mac",
            "Windows": "win",
            "Linux": "glnx",
        }[platform.system()]
    except KeyError:
        raise UnsupportedArchError(sys.platform)

    if arch == "mac":
        if platform.processor() == "arm":
            arch += "a"
        else:
            arch += "i"
        arch += "64"
    else:
        if sys.maxsize > 2**32:
            arch += "64"
        else:
            arch += "32"

    return arch


def macos_version():
    ver = platform.platform().split("-")[1]
    ver = tuple(map(int, ver.split(".")))
    return ver


def guess_prefix():
    if "MATLAB_RUNTIME_PATH" in os.environ:
        return os.environ["MATLAB_RUNTIME_PATH"]
    arch = guess_arch()
    if arch[:3] == "win":
        return "C:\\Program Files\\MATLAB\\MATLAB Runtime\\"
    if arch[:4] == "glnx":
        return "/usr/local/MATLAB/MATLAB_Runtime"
    if arch[:3] == "mac":
        return "/Applications/MATLAB/MATLAB_Runtime"
    assert False


# ----------------------------------------------------------------------
#   INSTALLERS
# ----------------------------------------------------------------------


def translate_version(version):
    if version[:1] == "R":
        return version
    if version in RUNTIME_VERSION_MAP:
        return RUNTIME_VERSION_MAP[version]
    year, release = version.split(".")
    return "R20" + year + {"1": "a", "2": "b"}[release]


def guess_version(version, arch=None):
    arch = arch or guess_arch()
    if version.lower() == "latest":
        year = str(datetime.now().year)
        for release in ("b", "a"):
            maybe_version = "R" + year + release
            try:
                guess_installer(maybe_version)
                version = maybe_version
                break
            except VersionNotFoundError:
                continue
        if version == "latest":
            # No version found for current year, use latest known version
            version = next(iter(
                sorted(INSTALLERS[arch].keys(), reverse=True)
            ))
    return translate_version(version)


def guess_installer(version, arch=None):
    arch = arch or guess_arch()
    error = VersionNotFoundError(f"No {version} installer found for Win{arch}")
    version = translate_version(version)
    if version in INSTALLERS[arch]:
        return INSTALLERS[arch][version]
    else:
        A, V, E = arch, version, "zip"
        fmt = dict(version=V, arch=A, ext=E)
        for U in reversed(range(11)):
            maybe_installer = TEMPLATE2.format(update=U, **fmt)
            if url_exists(maybe_installer):
                INSTALLERS[A][V] = maybe_installer
                return maybe_installer
        raise error


# ----------------------------------------------------------------------
#   KNOWN VERSIONS AND OTHER INFO
# ----------------------------------------------------------------------


RUNTIME_VERSION_MAP = {
    # Starting with R2023b, the runtime version scheme matches the
    # matlab release scheme, i.e., 23.2 === R2023b.
    # This dictionary contains a "runtime to matlab" version map for
    # versions prior to R2023b.
    "9.14": "R2023a",
    "9.13": "R2022b",
    "9.12": "R2022a",
    "9.11": "R2021b",
    "9.10": "R2021a",
    "9.9": "R2020b",
    "9.8": "R2020a",
    "9.7": "R2019b",
    "9.6": "R2019a",
    "9.5": "R2018b",
    "9.4": "R2018a",
    "9.3": "R2017b",
    "9.2": "R2017a",
    "9.1": "R2016b",
    "9.0.1": "R2016a",
    "9.0": "R2015b",
    "8.5.1": "R2015aSP1",
    "8.5": "R2015a",
    "8.4": "R2014b",
    "8.3": "R2014a",
    "8.2": "R2013b",
    "8.1": "R2013a",
    "8.0": "R2012b",
    "7.17": "R2012a",
}

RELEASE_UPDATE_MAP = {
    "R2024b": "5",
    "R2024a": "7",
    "R2023b": "10",
    "R2023a": "7",
    "R2022b": "10",
    "R2022a": "8",
    "R2021b": "7",
    "R2021a": "8",
    "R2020b": "8",
    "R2020a": "8",
    "R2019b": "9",
    "R2019a": "9",
}

INSTALLERS = {
    "win64": {},        # Windows 64 bits
    "win32": {},        # Windows 32 bits
    "glnxa64": {},      # Linux 64 bits
    "glnx86": {},       # Linux 32 bits
    "maci64": {},       # Mac Intel 64 bits
    "maca64": {},       # Mac ARM 64  bits
}

# Links @ https://uk.mathworks.com/products/compiler/matlab-runtime.html

# Links for versions >= R2019a
TEMPLATE2 = (
    "https://ssd.mathworks.com/supportfiles/downloads/{version}"
    "/Release/{update}/deployment_files/installer/complete/{arch}"
    "/MATLAB_Runtime_{version}_Update_{update}_{arch}.{ext}"
)
# Links for versions < R2019a
TEMPLATE1 = (
    "https://ssd.mathworks.com/supportfiles/downloads/{version}"
    "/deployment_files/{version}/installers/{arch}"
    "/MCR_{version}_{arch}_installer.{ext}"
)

# NOTE:
#   The (recent) MacOS link point to .dmg files, or to zip files that
#   only contain a dmg. However, replacing .dmg (or .dmg.zip) with .zip
#   allows an archive that contain a binary installer to be obtained.
#   We need this installer to be able to pass command line arguments.

# ----------------------------------------------------------------------
#   WINDOWS INSTALLERS
# ----------------------------------------------------------------------


A = "win64"
E = "zip"
for V, U in RELEASE_UPDATE_MAP.items():
    INSTALLERS[A][V] = TEMPLATE2.format(version=V, update=U, arch=A, ext=E)

E = "exe"
for Y in range(12, 19):
    for R in ("a", "b"):
        V = f"R20{Y}{R}"
        INSTALLERS[A][V] = TEMPLATE1.format(version=V, arch=A, ext=E)

A = "win32"
E = "exe"
for Y in range(12, 16):
    for R in ("a", "b"):
        V = f"R20{Y}{R}"
        INSTALLERS[A][V] = TEMPLATE1.format(version=V, arch=A, ext=E)


# ----------------------------------------------------------------------
#   LINUX INSTALLERS
# ----------------------------------------------------------------------


A = "glnxa64"
E = "zip"
for V, U in RELEASE_UPDATE_MAP.items():
    INSTALLERS[A][V] = TEMPLATE2.format(version=V, update=U, arch=A, ext=E)

for Y in range(12, 19):
    for R in ("a", "b"):
        V = f"R20{Y}{R}"
        INSTALLERS[A][V] = TEMPLATE1.format(version=V, arch=A, ext=E)

A = "glnx86"
E = "zip"
INSTALLERS[A]["R2012a"] = TEMPLATE1.format(version=V, arch=A, ext=E)


# ----------------------------------------------------------------------
#   MACOS INSTALLERS
# ----------------------------------------------------------------------


A = "maci64"
E = "zip"
for V, U in RELEASE_UPDATE_MAP.items():
    INSTALLERS[A][V] = TEMPLATE2.format(version=V, update=U, arch=A, ext=E)

for Y in range(12, 19):
    for R in ("a", "b"):
        V = f"R20{Y}{R}"
        INSTALLERS[A][V] = TEMPLATE1.format(version=V, arch=A, ext=E)

A = "maca64"
E = "zip"
for V in ("R2023b", "R2024a", "R2024b"):
    U = RELEASE_UPDATE_MAP[V]
    INSTALLERS[A][V] = TEMPLATE2.format(version=V, update=U, arch=A, ext=E)
