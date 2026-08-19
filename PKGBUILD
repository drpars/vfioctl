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
# transcripts go to $XDG_STATE_HOME/vfioctl, and the only other writer is
# core/sysfile.py, which
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
# THE pkgver FORMULA IS WRITTEN TWICE AND MUST STAY ONE FACT. core/provenance.py
# re-derives r<count>.g<short sha> from git so that `vfioctl doctor` can say
# whether the running clone and the installed package are the same code (K20).
# An installed package carries no .git, so there is no shared place to read it
# from; if the two lines below change, that module changes with them.
#
# WHY THE FILE LIST COMES FROM GIT AND NOT FROM A LIST KEPT BY HAND. It used to
# read `cp -r core data guest profiles vfioctl`, which was true the day it was
# written and silently untrue afterwards: a new top-level directory would not be
# packaged at all, and the package would install, run, and fail somewhere far
# away from here. Deriving the list from git reverses the failure mode -- what
# goes wrong now is that something not meant to ship lands in /usr/lib and does
# nothing. The exclusion list below is still kept by hand, and that is fine for
# the same reason: forgetting to exclude a file costs a harmless copy, while
# forgetting to include one costs a broken install. Two things fall out for
# free: git carries the executable bit, so a new executable is no longer
# installed 644 by a chmod line that names two paths, and byte-code caches
# cannot ship because git never tracked them.
#
# GIT PICKS THE NAMES, THE WORKING TREE SUPPLIES THE BYTES. `git archive HEAD`
# would make the package exactly equal its own pkgver, and it was not used: a
# developer building an uncommitted change would then install something other
# than what they are looking at, which is a worse surprise than a version
# string that is one commit approximate. The gap is not left silent -- package()
# warns when the tree is dirty and `vfioctl doctor` says so afterwards.
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

	git rev-parse --git-dir >/dev/null 2>&1 || {
		error "dosya listesi git'ten türetiliyor; burası bir checkout değil"
		return 1
	}

	# WHAT A DIRTY TREE ACTUALLY DOES, SAID THE RIGHT WAY ROUND. Only a file
	# that was never `git add`ed is missing from the index and therefore never
	# reaches the loop below. Every other kind of dirt SHIPS, carrying its
	# working-tree bytes -- names come from the index, bytes come from the disk
	# (see the header). So the line below warns about what the package is about
	# to contain, not about what it will leave out: pkgver names HEAD either
	# way, and that gap is the thing worth saying while it can still be fixed.
	local dirt
	dirt=$(git status --porcelain | grep -c .) || true
	(( dirt == 0 )) || warning "$dirt yol commit edilmemiş; izlenenler bu hâlleriyle pakete giriyor, pkgver=$pkgver onları adlandırmıyor"

	local libdir="$pkgdir/usr/lib/$pkgname"
	local entry mode path perm

	# -z, so a path with a space or a newline in it stays one record instead
	# of becoming two half-installed files.
	while IFS= read -r -d '' entry; do
		mode=${entry%% *}
		path=${entry#*$'\t'}

		# Documentation and packaging: installed elsewhere below, or not at
		# all. Anything not named here ships, which is the safe direction.
		case "$path" in
		.git*|CLAUDE.md|LICENSE|PKGBUILD|README.md|docs/*) continue ;;
		esac

		case "$mode" in
		100644) perm=644 ;;
		100755) perm=755 ;;
		*)
			# A symlink (120000) or a submodule (160000). `install` would
			# copy the target's bytes and call it the same thing, which is
			# the silent difference this whole list exists to remove.
			error "$path: git modu $mode -- bu paket onu nasıl kuracağını bilmiyor"
			return 1
			;;
		esac

		install -Dm"$perm" "$path" "$libdir/$path"
	done < <(git ls-files -sz)

	# install -D creates the leading directories; their mode is set here
	# rather than assumed, because it is the one thing -D does not spell out.
	find "$libdir" -type d -exec chmod 755 {} +

	install -dm755 "$pkgdir/usr/bin"
	ln -s "/usr/lib/$pkgname/vfioctl" "$pkgdir/usr/bin/$pkgname"

	install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
	install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
	install -Dm644 docs/bluetooth-code10.md \
		"$pkgdir/usr/share/doc/$pkgname/bluetooth-code10.md"
}
