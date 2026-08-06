# Maintainer: Muhammed Izzet Saglam <m.izzetsaglam@vuhuv.com>
#
# WHY THE WHOLE TREE GOES TO ONE DIRECTORY AND THE ENTRY POINT IS A SYMLINK.
# Four modules resolve their assets from their own location: core/profile.py
# (profiles/), core/lookingglass.py (guest/windows/looking-glass.ps1),
# core/hostfiles.py (data/50-vfio-handover) and guest/build.py (templates/).
# A package that scatters those across /usr/lib and /usr/share breaks all four.
# So the tree stays whole under /usr/lib/vfioctl and only the entry point is
# published, as a symlink -- measured: Python resolves a symlinked script before
# it sets sys.path[0], so `from core import ...` still finds the tree. A *copy*
# of the entry script in /usr/bin does not work; the symlink is not cosmetic.
#
# NOTHING IS WRITTEN INSIDE THE TREE AT RUNTIME, which is what makes a
# root-owned install directory safe: guest/build.py stages under ~/.images,
# selftest logs to /tmp, and the only other writer is core/sysfile.py, which
# goes to /etc through sudo. If a future subcommand wants to write next to its
# own code, this layout is what it breaks.
#
# WHY THERE IS NO source=() ARRAY. The repository is public, so a source line
# would resolve; what is missing is not the ability but the intent. vfioctl is
# deliberately not published to the AUR: profiles/ holds exactly one machine, so
# the gate would come up closed for nearly every AUR user -- not broken, doctor
# still runs and says why, but not what "install this package" implies. And the
# one thing an AUR entry would buy, `pacman -Syu` carrying updates, is spelled
# `git pull && makepkg -si` here, against the user's own clone. So this PKGBUILD
# builds the checkout it sits in: clone, then `makepkg -si` in the clone. The
# decision reopens the day a second machine lands in profiles/ and a run is
# measured on it; the diff is small and local -- add source=() plus sha256sums=(),
# and change the two `cd "$startdir"` lines to the extracted source directory.
# Nothing else here is checkout-specific.
#
# WHY pkgver IS COMPUTED AT PARSE TIME instead of in a pkgver() function.
# makepkg rewrites the pkgver= line of the PKGBUILD in place when a pkgver()
# function is present. That would leave the repository dirty after every build
# and put a mechanical edit into a history where every commit means something.
# Computing it while the PKGBUILD is sourced gives the same answer and leaves
# the file alone.
#
# WHY depends= IS SHORT. The tool's standing rule is that it never installs a
# package: a missing half is measured and the command is printed (see CLAUDE.md,
# "Paket kurulmaz"). Only what every subcommand needs is a hard dependency;
# the rest is optdepends, worded as the feature it unlocks.

pkgname=vfioctl
pkgver=$(
	cd "$startdir" 2>/dev/null &&
		git rev-parse --git-dir >/dev/null 2>&1 &&
		printf 'r%s.g%s' "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)" ||
		printf '0'
)
pkgrel=1
pkgdesc="Install, check and drive a VFIO GPU passthrough setup for a Windows guest"
arch=('any')
url="https://github.com/drpars/vfioctl"
license=('MIT')
depends=(
	'python'  # >= 3.11 for tomllib; profiles are TOML read from the stdlib
	'libvirt' # virsh: doctor, status, selftest and every guest subcommand
)
optdepends=(
	'qemu-img: building a guest from scratch (vfioctl guest build)'
	'libisoburn: building the unattended install ISO (xorriso)'
	'looking-glass: the host half of Looking Glass, and the version check'
	'nvidia-utils: nvidia-smi, used by selftest to name what still holds the card'
	'swtpm: TPM for the Windows 11 guest'
	'edk2-ovmf: UEFI firmware for the guest'
)
source=()

package() {
	cd "$startdir"

	local libdir="$pkgdir/usr/lib/$pkgname"
	install -dm755 "$libdir"
	cp -r core data guest profiles vfioctl "$libdir/"

	# Byte-code caches from running out of the checkout must not ship: they
	# carry the developer's paths and pacman would own files Python rewrites.
	find "$libdir" -type d -name __pycache__ -prune -exec rm -rf {} +

	find "$libdir" -type d -exec chmod 755 {} +
	find "$libdir" -type f -exec chmod 644 {} +
	chmod 755 "$libdir/vfioctl" "$libdir/data/50-vfio-handover"

	install -dm755 "$pkgdir/usr/bin"
	ln -s "/usr/lib/$pkgname/vfioctl" "$pkgdir/usr/bin/$pkgname"

	install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
	install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
	install -Dm644 docs/bluetooth-code10.md \
		"$pkgdir/usr/share/doc/$pkgname/bluetooth-code10.md"
}
