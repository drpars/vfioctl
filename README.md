# vfioctl

🇹🇷 Bir dizüstünün harici ekran kartını (dGPU) Windows misafirine devreden
VFIO passthrough kurulumunu kuran, ölçen ve süren CLI aracı.
🇬🇧 CLI tool that installs, checks and drives a VFIO passthrough setup which
hands a laptop's discrete GPU to a Windows guest.

> **Durum: yapım aşamasında.** Bugün burada **donanım kapısı** (`vfioctl
> doctor`) ve **misafir inşası** (`guest/`) var. Host tarafı — devir hook'u,
> udev kuralları, Looking Glass'ın host yarısı — hâlâ
> [archsetup](https://github.com/drpars/archsetup) içinde ve buraya taşınacak.

## Neden ayrı bir proje

Passthrough kurulumu iki yarıya bölünmüştü: host yapılandırması bir Arch
kurulum aracının içinde, misafir tarafı bir not klasöründe. Bölünme, kurulumun
ikinci bir makinede koşmasını yapısal olarak imkânsız kılıyordu — dört
yapılandırma dosyası için baştan sona bir kurulum aracı koşmak gerekiyordu.
Ayıran test şu: *bu dosya neden var?* Tek gerekçesi passthrough olan her şey
buraya gelir; passthrough olmasa da doğru olanlar (libvirt paketleri, `default`
ağı, grup üyelikleri) archsetup'ta kalır.

## Kapsam — ve kapsamda olmayan

Araç **kendine yeter, ama evrensel değildir.** Hedef sınıfı: dGPU + iGPU
taşıyan, MUX'lu, dGPU'sunun IOMMU grubu izole olan ASUS dizüstüler.

Bunun bir vaat değil bir **kapı** olması tasarımın parçası:

- `doctor` her makinede koşar, hiçbir şey yazmaz, neyin uymadığını tek tek
  söyler — başka bir makineye taşımak isteyenin ihtiyacı budur.
- Yazan hiçbir alt komut profil eşleşmesi olmadan koşmaz. `--force` yoktur:
  yarı çalışan bir passthrough kurulumu, hiç kurulmamış olmaktan kötüdür.

Kapsam dışı: tek GPU'lu makineler (host ekranını kaybeden **başka** bir
tasarım), AMD dGPU'ların reset bug'ı, Hyprland dışı compositor'lar.

## Bugün ne var

```
vfioctl                   # giriş noktası: doctor, profiles
core/                     # probe (makineyi okur) + profile + doctor/gate
profiles/                 # tanınan makineler, birer .toml
guest/
├── build.py              # boş diskten konsol oturumu açık Windows'a, gözetimsiz
├── templates/            # autounattend.xml, domain.xml, SetupComplete.cmd
└── windows/              # misafirde SYSTEM olarak koşan kurulum betikleri
```

### Kapı

```sh
./vfioctl doctor          # bu makine ne, ve buraya yazabilir miyiz
./vfioctl profiles        # hangi makineleri üstleniyoruz
```

`doctor` hiçbir şey yazmaz ve **her makinede** koşar. Profil eşleşirse sert ve
yumuşak ölçütleri tek tek raporlar; eşleşmezse donanımı keşfeder ve profil
yazmaya değip değmeyeceğini söyler. Çıkış kodu: 0 kapı açık, 1 kapalı,
2 böyle bir profil yok.

**Yeni bir makine eklemek:** `profiles/` altındaki `.toml`'u kopyala, DMI
dizgelerini ve PCI kimliklerini değiştir, `./vfioctl doctor` koş. Zorunlu olan
iki şey var — kartın IOMMU grubunda kartından başka bir şey olmaması, ve host'un
ekranını taşıyacak ikinci bir GPU bulunması.

### Misafir inşası

```sh
./guest/build.py build --user <ad> --password-file <yol>
./guest/build.py status
./guest/build.py clean
```

Bu makinede ölçüldü: boş diskten ajanı ulaşılabilir, konsol oturumu açık bir
Windows 11 Pro 25H2'ye **7 dk 33 sn**, elle adım yok.

`guest/windows/` altındaki üç betik (sanal ekran sürücüsü, Looking Glass host,
ekran topolojisi) idempotent ve çalışıyor, ama henüz elle sürülüyor — onları
sırayla koşan sürücü kod sıradaki iş.

**Her dosyanın başlığında gerekçeli bir açıklama var.** Neyin neden öyle
yazıldığı — hangi API'nin sessizce başarısız olduğu, hangi sıranın zorunlu
olduğu — koddan değerli; oralar okunmadan değiştirilmemeli.

## Yol haritası

| Faz | İçerik |
|---|---|
| 0 | taşınma + iskelet ✅ |
| 1 | kapı: donanım profili biçimi, `doctor` ✅ ← **buradayız** |
| 2 | host kurulumu: devir hook'u, udev kuralları, Looking Glass host yarısı |
| 3 | misafir inşasının kalan yarısı: üç PS1 betiğini süren kod |
| 4 | envanter, ek cihaz devri (Bluetooth, ikinci NVMe) |

## Gereksinimler

`libvirt`, `qemu`, `edk2-ovmf`, `swtpm`, `virtio-win`, `xorriso`, Python 3.11+
(`tomllib` için). Depo dışı bağımlılık yok.
Windows kurulum ISO'su kullanıcının kendisi tarafından sağlanır — bu depo
misafire ait hiçbir dosyayı indirmez.

## Sırlar

Kişisel hiçbir değer bu depoya girmez: hesap adı, parola, yerel ayar ve ISO
yolu çalışma anında verilir. Üretilen `autounattend.xml` ve yardımcı ISO
`~/.images/<domain>-unattend/` altında 0700 dizinde, makine-yerel kalır.
Depoya giren **şablon**dur.

Yeni bir klonda bir kez, sızıntı taramasını bağlamak için:

```sh
git config core.hooksPath .githooks
```

## Lisans

Henüz seçilmedi.
