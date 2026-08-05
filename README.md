# vfioctl

🇹🇷 Bir dizüstünün harici ekran kartını (dGPU) Windows misafirine devreden
VFIO passthrough kurulumunu kuran, ölçen ve süren CLI aracı.
🇬🇧 CLI tool that installs, checks and drives a VFIO passthrough setup which
hands a laptop's discrete GPU to a Windows guest.

> **Durum: yapım aşamasında.** Bugün burada **donanım kapısı** (`doctor`),
> **host kurulumu** (`install` / `uninstall` / `selftest`) ve **misafir
> inşası** (`guest/`) var. Kalan: misafir tarafının üç betiğini süren kod
> (Faz 3) ve envanter (Faz 4).

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
tasarım), AMD dGPU'ların reset bug'ı.

### Seans yarısı: yazılmaz, ölçülür

Kurulumun taşıyıcı koşullarından biri, **grafik oturumunun dGPU'ya hiç
dokunmaması.** Ölçüt tam olarak şu: devir anında oturumun hiçbir süreci
dGPU'nun DRM düğümünü (`/dev/dri/card*`) ya da `/dev/nvidia*`'ı açık
tutmamalı — ve sabitleme compositor **başlamadan önce** kurulmuş olmalı, çünkü
cihaz seçimi süreç başlarken bir kez okunur.

Bu yarıyı vfioctl **yazmaz**: sahibi kullanıcının kendi masaüstü
yapılandırmasıdır (bu makinede ayrı bir dotfiles deposu). Ama **ölçer** —
`doctor` her makinede bakar, ve geçmiyorsa ölçütü ve nereye kurulacağını,
**neyin ölçülmediğini yazarak** basar.

Ölçülmüş olan: Hyprland (`AQ_DRM_DEVICES`), bu makinede. Ölçülmemiş olan:
KWin, mutter ve diğerleri — KWin'in kendi DRM cihaz seçici değişkeni var
(`KWIN_DRM_DEVICES`), bu araç onu denemedi. Buna karşılık kurulumun iki
parçası compositor'den bağımsız: dGPU'nun DRM düğümünü seat envanterinden
çıkaran udev kuralı **logind** üzerinden çalışır (mekanizma compositor'ün
değil), ve seans değişkenlerinin dördünün üçü glvnd + Vulkan loader'ına aittir,
her masaüstünde aynıdır.

Duruş bu yüzden "yalnızca Hyprland çalışır" değil: **ölçüt söylenir, ölçüm
kullanıcının makinesinde `doctor` ile yapılır.** Başka bir compositor'de
çalışacağı vaat edilmiyor; çalışıp çalışmadığını o makinede okumanın yolu var.

## Bugün ne var

```
vfioctl                   # giriş noktası: doctor, profiles, install, uninstall, selftest
core/                     # probe (makineyi okur) + profile + doctor/gate
│                         # + session (seans yarısını ölçer, yazmaz)
│                         # + hostfiles/install (host tarafı) + selftest
data/50-vfio-handover     # devri yapan libvirt hook'u
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

Ayrı bir bölüm de **seans yarısını** ölçer: seat0'daki etkin oturum, kartı
tutan bir süreç olup olmadığı, ve iGPU symlink'i. Bu denetimler **kapıyı
etkilemez** ve ölçülemediklerinde "geçmedi" değil **"ölçülemedi"** derler —
kapı makineye bakar (kalıcı soru), seans denetimi ana bakar (her boot değişir).
Compositor denetimi kapıya konsaydı kapı kendi kendini kilitlerdi: `selftest`
düz VT'den koşulur, orada ölçülecek compositor yoktur.

**Yeni bir makine eklemek:** `profiles/` altındaki `.toml`'u kopyala, DMI
dizgelerini ve PCI kimliklerini değiştir, `./vfioctl doctor` koş. Zorunlu olan
iki şey var — kartın IOMMU grubunda kartından başka bir şey olmaması, ve host'un
ekranını taşıyacak ikinci bir GPU bulunması.

### Host kurulumu

```sh
./vfioctl install --check   # hiçbir şey yazma: /etc ile aranda ne fark var
./vfioctl install           # sekiz dosyayı yaz, sonra makineyi okuyup doğrula
./vfioctl uninstall         # geri al
```

Kurulan sekiz dosya: iGPU'ya kararlı ad veren udev kuralı, dGPU'yu seat
envanterinden çıkaran kural, devir hook'u + `vfio.conf`, SDDM greeter'ını
karttan uzak tutan Xorg anahtarı, ve Looking Glass'ın host yarısı (kvmfr'nin
`modules-load`/`modprobe`/udev üçlüsü). Dokuzuncu parça `qemu.conf`'un
`cgroup_device_acl`'i — **üretilen değil düzenlenen** tek yer, ve orada
yalnızca kendi satırına dokunulur.

Dört şey bilerek yapılmıyor:

- **Paket kurulmaz.** Looking Glass'ın iki yarısı AUR'da; `install` onları
  ölçer ve yoksa komutu basıp durur. Olmayan bir modül için yapılandırma
  yazmak, sessiz başarısızlığın ders kitabı hâlidir.
- **Karta dokunulmaz.** Ne `install` ne `selftest` bir PCI cihazını bağlar,
  çözer ya da probe eder. Devri libvirt hook'u yapar — tek yazar, bilerek.
- **`qemu.conf`'un `user`/`group` satırları yazılmaz**, yalnızca raporlanır:
  onların gerekçesi passthrough değil.
- **Seans yarısı yazılmaz**, ölçülür ve **uyarı** olarak basılır (reddedilmez:
  compositor kartı tutuyorsa `install` tam da bunun çaresidir — seat kuralı
  daha bir dakika önce diskte yoktu). → "Seans yarısı: yazılmaz, ölçülür"

`install --check` kalıcı bir araçtır, tek seferlik bir doğrulama değil: `/etc`
kayar, paket güncellemesi dosya geri koyar, biri gecenin üçünde VT'den kural
düzenler. Sorusu şu — *bu makine hâlâ kurduğumuz şey mi?*

### Devri sınamak

```sh
./vfioctl selftest --preflight   # yalnızca okuma: devir şu an geçer miydi
./vfioctl selftest               # 5 tur art arda, yargılanmış ve günlüklenmiş
./vfioctl selftest --rounds 1    # hızlı bakış
```

Bu makinede ölçüldü (2026-08-05): **5/5 tur temiz, 3 dk 9 sn**, yoklayıcı
boyunca canlı (waybar +203 jiffies), hook beklemesi her turda 0, journal'da tek
bir `non-zero usage count` yok. Misafir her turda 15–20 sn'de ayağa kalktı.

**Düz bir VT'den (Ctrl+Alt+F3) ya da başka bir makineden ssh ile koşulur** —
aradığı arıza grafik oturumunu öldüren arızadır, o oturumun içindeki bir kabuk
onunla birlikte ölür. Çıktı `/tmp/vfioctl-selftest.log`'a da yazılır, sonuç bu
yüzden okuyucudan sağ çıkar.

**Yoklayıcı testin parçasıdır.** Kartı saniyede bir açan kısa ömürlü bir süreç
(bu makinede waybar'ın `gputemp` modülü) devri deterministik olmaktan çıkarıp
zar atışına çeviriyordu; düzeltmenin işe yarayıp yaramadığı ancak öyle bir
süreç canlıyken ölçülebilir. Bu yüzden `selftest` yoklayıcı yoksa ya da
durdurulmuşsa koşmayı reddeder ve her turda onun cpu deltasını basar — sıfır
delta turu "sonuç geçersiz" yapar. `--no-poller` bilerek sessiz taban çizgisi
içindir, kabul turu değildir.

**`selftest` seans yarısının ölçüldüğü yer değildir.** Preflight'ı "compositor
kartı şu an tutuyor mu" diye bakar ve tutuyorsa turu hiç başlatmaz; ama neyin
gerektiğini ve nereye kurulacağını söyleyen komut `doctor`. `selftest`'in
sorusu daha dar ve daha pahalı: gerçek bir devir, gerçek bir misafirle, art
arda beş kez.

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
| 1 | kapı: donanım profili biçimi, `doctor`, seans yarısının ölçümü ✅ |
| 2 | host kurulumu: devir hook'u, udev kuralları, Looking Glass host yarısı ✅ ← **buradayız** |
| 3 | misafir inşasının kalan yarısı: üç PS1 betiğini süren kod |
| 4 | envanter, ek cihaz devri (Bluetooth, ikinci NVMe) |

## Gereksinimler

`libvirt`, `qemu`, `edk2-ovmf`, `swtpm`, `virtio-win`, `xorriso`, Python 3.11+
(`tomllib` için). Looking Glass için AUR'dan `looking-glass` +
`looking-glass-module-dkms` (DKMS çekirdek başlıklarını ister). Bu araç paket
kurmaz — eksik olanı ölçer ve komutu basar. Python tarafında depo dışı
bağımlılık yok.
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
