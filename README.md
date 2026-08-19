# vfioctl

🇹🇷 Bir dizüstünün harici ekran kartını (dGPU) Windows misafirine devreden
VFIO passthrough kurulumunu **kuran, ölçen ve süren** CLI aracı.
🇬🇧 CLI tool that installs, checks and drives a VFIO passthrough setup which
hands a laptop's discrete GPU to a Windows guest.

> **Durum: yol uçtan uca çalışıyor, bu makinede ölçülmüş hâliyle.** Boş bir
> diskten, kartı devredilmiş ve Looking Glass'ın o kart üzerinde yakaladığı bir
> Windows misafirine kadar her adım burada — donanım kapısı (`doctor`), host
> kurulumu (`install`/`selftest`), misafir inşası ve sürülmesi (`guest`), ek
> cihaz devri (`inventory`, `guest usb`, `guest nvme`). Dört fazın dördü de
> kapandı.

**İki belge var:** bu dosya *ne yapılır* sorusunu cevaplar. *Neden böyle*
sorusunun cevabı — elenen yollar, olmayan bayrakların gerekçesi, her kararın
önlediği arıza — [docs/TASARIM.md](docs/TASARIM.md)'dedir. Bir şeyi
değiştirmeden önce orası okunur.

---

## Gereksinimler

`libvirt`, `qemu`, `edk2-ovmf`, `swtpm`, `virtio-win`, `xorriso`, Python 3.11+
(`tomllib` için). Looking Glass için AUR'dan `looking-glass` +
`looking-glass-module-dkms` (DKMS çekirdek başlıklarını ister).

**Bu araç paket kurmaz** — eksik olanı ölçer ve komutu basar. Python tarafında
depo dışı bağımlılık yok. Windows kurulum ISO'su kullanıcının kendisi tarafından
sağlanır; bu depo misafire ait hiçbir dosyayı indirmez.

**Donanım sınıfı:** dGPU + iGPU taşıyan, MUX'lu, dGPU'sunun IOMMU grubu izole
olan ASUS dizüstüler. Makinenizin uyup uymadığını `vfioctl doctor` söyler —
tahmin gerekmez. Kapsam dışı: tek GPU'lu makineler, AMD dGPU'ların reset bug'ı.
→ [Kapsam](docs/TASARIM.md#kapsam--kapı-vaat-değil)

## Kurulum

```sh
git clone https://github.com/drpars/vfioctl && cd vfioctl && makepkg -si
```

`makepkg` `base-devel` ister; `depends` yalnızca `python` ve `libvirt`, geri
kalan her şey `optdepends`.

Kurulduktan sonra komut `vfioctl` (ağaç `/usr/lib/vfioctl`'e kurulur,
`/usr/bin/vfioctl` ona symlink'tir). Klondan da koşabilirsiniz: `./vfioctl` —
ikisi aynı şeydir, **ama bir istisnayla**: `install --check`'in hükmü kurulu
koda göredir, o yüzden doğrulama kurulu komutla yapılır.

**Otomatik güncelleme kanalı yoktur.** Araç AUR'da değil, yani `pacman -Syu` onu
tazelemez; güncelleme `git pull && makepkg -si`.

Yeni bir klonda bir kez, sızıntı taramasını bağlamak için:

```sh
git config core.hooksPath .githooks
```

---

## Kullanım — sıfırdan çalışan bir misafire

Aşağıdaki sıra bu makinede **uçtan uca koşuldu ve geçti** (2026-08-19). Örnek
domain adı `win11-nvme`; kendi adınızı `--name` ile verirsiniz.

Toplam süre: kurulum ~8 dk (gözetimsiz), misafir tarafı ~7 dk.

### 1. Bu makine uygun mu

```sh
vfioctl doctor
```

Hiçbir şey yazmaz, **her makinede koşar**. Çıkış kodu: `0` kapı açık, `1`
kapalı, `2` bu makine için profil yok. Profil eşleşmezse donanımı keşfeder ve
profil yazmaya değip değmeyeceğini söyler.

Rapor üç bölümdür: **sert ölçütler** (kapıyı belirler), **yumuşak ölçütler**, ve
**seans yarısı** — grafik oturumunuzun dGPU'ya dokunup dokunmadığı. Sonuncusu
kapıyı etkilemez ama devri belirler; geçmiyorsa `doctor` ne gerektiğini ve
nereye kurulacağını basar. Bu yarıyı vfioctl **yazmaz**, sahibi sizin masaüstü
yapılandırmanızdır. → [Seans yarısı](docs/TASARIM.md#seans-yarısı-yazılmaz-ölçülür)

**Yeni bir makine eklemek:** `profiles/` altındaki `.toml`'u kopyalayın, DMI
dizgelerini ve PCI kimliklerini değiştirin, `doctor` koşun. Zorunlu iki şey:
kartın IOMMU grubunda kartından başka bir şey olmaması, ve host'un ekranını
taşıyacak ikinci bir GPU bulunması.

### 2. Host tarafını kur

```sh
vfioctl install --check    # önce: /etc ile aranda ne fark var
vfioctl install            # dokuz parçayı yaz, sonra makineyi okuyup doğrula
```

Yazılan sekiz dosya: iGPU'ya kararlı ad veren udev kuralı, dGPU'yu seat
envanterinden çıkaran kural, devir hook'u + `vfio.conf`, SDDM greeter'ını
karttan uzak tutan Xorg anahtarı, ve Looking Glass'ın host yarısı (kvmfr'nin
`modules-load`/`modprobe`/udev üçlüsü). Dokuzuncu parça `qemu.conf`'un
`cgroup_device_acl` satırı — üretilen değil **düzenlenen** tek yer.

Araç root koşmaz; yetki yazma anında, komut başına alınır. Looking Glass eksikse
**kurmaz** — ölçer ve kurulum komutunu basıp durur.

`install --check` tek seferlik bir doğrulama değil, kalıcı bir araçtır: `/etc`
kayar, paket güncellemesi dosya geri koyar. Sorusu şu — *bu makine hâlâ
kurduğumuz şey mi?* → [install](docs/TASARIM.md#install--dört-şey-bilerek-yapılmıyor)

`install` bittiğinde makineyi **kendisi okuyup doğrular**: udev kurallarını
tazeler ve tetikler, kvmfr'nin yüklü olup olmadığına ve boyutuna bakar, koşan
Xorg'da NVIDIA GPU screen'i kaldı mı diye sorar. **Yeniden başlatma gerekiyorsa
bunu size o söyler** — körlemesine reboot etmeniz gerekmez.

### 3. Neyi devredeceğine karar ver

```sh
vfioctl inventory
```

PCI ve USB cihazlarını, her birinin **host'a bedeliyle** listeler. Yalnızca
rapordur, hiçbir şey uygulamaz. İşaretler: `✓` ölçülen bedel yok · `!` bedeli
var, yine de devredilir · `✗` reddedilir.

Sistem diskini fiziksel bir NVMe yapacaksanız (aşağıda kip 2) denetleyicinin
PCI adresini buradan alırsınız — ör. `0000:02:00.0`.

> **⚠ `✓` "boş" demek değildir.** Araç disk **içeriğini hiç okumaz**; veri dolu
> ama bağlı olmayan bir disk de `✓` alır. Kip 2'nin `EVET` onayı tam da bu
> yüzden var. → [inventory](docs/TASARIM.md#inventory--devir-birimi-retuyarı-işaret-kuralı)

### 4. Misafiri kur

Önce parola dosyası — komut satırına yazılmaz, `ps` çıktısına düşmesin:

```sh
mkdir -p ~/.images
(umask 077; cat > ~/.images/win11-nvme.pass)   # parolayı yaz, Ctrl+D ile bitir
```

`umask 077` dosyayı doğuşundan 0600 yapar, ve parola `cat`'in girdisinden
geçtiği için **kabuk geçmişine düşmez**. Dosyayı hiç istemiyorsanız
`--password-file`'ı atlayın: araç parolayı sorar.

Sonra tek komut. **İki kip var ve birbirini dışlarlar** — değişen tek şey bu
satır, gerisi (`passthrough`, `setup`, `status`, `clean`) ikisinde de aynı:

```sh
# Kip 1 — sistem diski bir qcow2 imajı
vfioctl guest --name win11-nvme --disk ~/.images/win11-nvme.qcow2 build \
  --size 64G \
  --win-iso ~/İndirilenler/win11.iso \
  --user pars --password-file ~/.images/win11-nvme.pass

# Kip 2 — sistem diski fiziksel bir NVMe denetleyicisi (adres: adım 3)
vfioctl guest --name win11-nvme build \
  --system-nvme 0000:02:00.0 \
  --win-iso ~/İndirilenler/win11.iso \
  --user pars --password-file ~/.images/win11-nvme.pass \
  --confirm-wipe
```

Gözetimsiz koşar: cevap dosyasını üretir, domain'i tanımlar, Windows'u kurar,
virtio sürücülerini ve qemu-guest-agent'ı yerleştirir. Bu makinede ölçüldü —
kip 1 **7 dk 33 sn**, kip 2 **8 dk 8 sn**, elle adım yok.

> **⚠ Kip 2 diski baştan bölümlüyor.** Komut diski **model + seri** ile yazar ve
> `EVET` yazmanızı bekler; `--confirm-wipe` o soruyu geçer. Terminal yoksa tur
> **reddedilir** — sorulacak kimse olmaması onay değildir.

Sık kullanılan diğer bayraklar: `--memory` (MiB, öntanımlı 8192), `--vcpu` (8),
`--locale` (`tr-TR`), `--timezone`, `--virtio-iso`, `--force` (tanımlıysa önce
temizle). Tamamı için `vfioctl guest build --help`.

### 5. Kurulum ortamını çıkar

Misafiri kapatın, sonra:

```sh
virsh -c qemu:///system shutdown win11-nvme
until [ "$(virsh -c qemu:///system domstate win11-nvme)" = "shut off" ]; do sleep 2; done
vfioctl guest --name win11-nvme eject --all
```

> **⚠ `virsh shutdown` işi zamanlar, yapmaz** — çıkış 0 ile hemen döner, misafir
> hâlâ koşuyordur. Beklemeden çağrılan `eject` "domain kapalı değil" der. Ölçüt
> `domstate`, komutun çıkış kodu değil.

**Bu adım atlanmaz.** `build` cevap dosyası ISO'sunu takıyor ve kendisi
çıkarmıyor; kalırsa sonraki açılış gözetimsiz Setup'ı **yeniden koşturabilir** —
ve cevap dosyası `DiskID 0`'ı siliyor. Varsayılan yalnız cevap dosyasını çıkarır;
`--all` kurulum ISO'sunu ve virtio-win'i de çıkarır. Hiçbir dosya silinmez,
ISO'lar yerinde durur.

Domain **kapalı** olmalı ve **bu aracın işaretini** taşımalı — `build`'in
tanımlamadığı bir domain'e dokunmaz.

### 6. Konsol oturumunu aç

```sh
virsh -c qemu:///system start win11-nvme
# ajan cevap verene kadar bekleyin: vfioctl guest --name win11-nvme status
vfioctl guest --name win11-nvme autologon \
  --user pars --password-file ~/.images/win11-nvme.pass
```

**Bu adım da atlanmaz, ve sebebi Windows'tur:** 11 25H2'nin OOBE temizliği bir
sonraki oturum kapanışında yeniden koşuyor ve `AutoAdminLogon`'u siliyor —
misafir kilit ekranında oturur. `build`'in *"autologon kalıcı"* çıktısı kendi
yaptığı yeniden başlatmayı ölçer, **tam bir kapat/aç turunu ölçmez**.

Looking Glass bir **oturumu** yakalar; açık konsol oturumu yoksa yakalanacak bir
şey de yoktur. Etkisi bir sonraki açılışta görünür.

### 7. Kartı domain'e ver

```sh
vfioctl guest --name win11-nvme passthrough        # geri almak: --off
```

Yalnızca **kapalı** bir domain'i düzenler ve `managed='no'` yazar — kartı
libvirt değil, devir hook'u taşır (tek yazar).

Başlatmadan önce ucuz ve yan etkisiz bir sonda:

```sh
virsh -c qemu:///system dumpxml win11-nvme \
  | VFIO_HOOK_CHECK=1 /etc/libvirt/hooks/qemu.d/50-vfio-handover
```

`result: handover` (rc 0) = hook devri üstlenecek. `HOST-MOUNTED` satırı
görünüyorsa **başlatmayın**. `result: unconfigured` (rc 2) hook'un
yapılandırılmadığını söyler.

### 8. Devri koş ve misafir tarafını kur

> **⚠ Devir turu düz bir VT'den koşulur** (`Ctrl`+`Alt`+`F3`), ya da başka bir
> makineden `ssh` ile. Sebep: aradığınız arıza grafik oturumunu öldüren
> arızadır, o oturumun içindeki bir kabuk onunla birlikte ölür.

```sh
vfioctl guest --name win11-nvme setup --start
```

`--start` domain'i başlatır (kartı hook devreder), ajanı bekler, sonra misafir
betiklerini sırayla sürer: **NVIDIA sürücüsü → sanal ekran (VDD) → Looking Glass
→ tek ekran topolojisi**. Sıra bir bağımlılıktır, keyfi değil.

Domain zaten kartla koşuyorsa riskli an geçmiştir; `--start` olmadan
masaüstünden de koşabilirsiniz. Günlük:
`$XDG_STATE_HOME/vfioctl/setup.log` (öntanımlı `~/.local/state/vfioctl/`).

Bu makinede ölçüldü: **7 dk 20 sn**, kart kod 10 → **kod 0**, VDD 2560x1440
kartın üstünde render ediyor, LG host servisi çalışıyor.
→ [setup](docs/TASARIM.md#setup--sıra-bir-bağımlılıktır)

### 9. Ekranı aç

Misafir koşarken, ana makinenin grafik oturumundan:

```sh
looking-glass-client app:shmFile=/dev/kvmfr0 win:fullScreen=yes
```

Bu makinede ölçülen çağrı biçimi budur. **Seçenekler pozisyonel yazılır**
(`modül:ad=değer`), kısa bayrak olarak değil: LG'nin kısa bool bayrakları değer
**atamaz**, mevcut değeri tersine çevirir — yani `-F yes` diye bir şey yoktur ve
sessizce yanlış sonucu verir.

Kalıcı ayar için `~/.config/looking-glass/client.ini`. **Kaçış tuşuna bakın:**
LG'nin varsayılanı Scroll Lock, ve birçok dizüstü klavyesinde o tuş fiziksel
olarak yoktur — o hâlde yakalama modundan çıkamazsınız. `[input]` bölümüne
klavyenizde var olan bir tuş yazın (ör. `escapeKey=KEY_INSERT`).

---

### Kurulumu sınamak

```sh
vfioctl selftest --preflight --domain win11-nvme   # yalnızca okuma
vfioctl selftest --domain win11-nvme               # 5 tur art arda
vfioctl selftest --rounds 1 --domain win11-nvme    # hızlı bakış
```

Gerçek bir devri, gerçek bir misafirle, art arda beş kez koşar ve yargılar.
**Düz VT'den koşulur.** Bu makinede ölçüldü (2026-08-18): **5/5 temiz**,
yoklayıcı boyunca canlı, journal'da tek bir `non-zero usage count` yok.

**Yoklayıcı testin parçasıdır:** kartı saniyede bir açan kısa ömürlü bir süreç
(bu makinede waybar'ın `gputemp` modülü) devri zar atışına çeviriyordu, ve
düzeltmenin işe yaradığı ancak öyle bir süreç canlıyken ölçülebilir. `selftest`
yoklayıcı durmuşsa **koşmayı reddeder**. `--no-poller` sessiz taban çizgisi
içindir, kabul turu değildir.

> **`--domain` zorunludur, öntanımlı değeri yoktur.** Verilmezse komut hiçbir
> şey yapmadan çıkar (rc 2), tanımlı domain'leri listeler ve eksik bayrağı
> eklenmiş çağrıyı basar. Eskiden `win11`'e öntanımlıydı; bu makinenin misafiri
> `win11-nvme` olunca eski ad çözünmeye devam etti, yani argümansız çağrı hata
> vermeden **başka bir misafirde** koşup sonucu kabul ölçütü diye raporlardı.

### Günlük kullanım

```sh
vfioctl guest --name win11-nvme status                       # domain + ajan
vfioctl guest --name win11-nvme usb --attach 8087:0032       # aygıt ödünç ver
vfioctl guest --name win11-nvme usb --detach 8087:0032       # geri al
vfioctl guest --name win11-nvme screenshot                   # ekran görüntüsü
```

`usb` koşan bir misafire aygıt (Bluetooth radyosu, oyun kolu, bellek, fare
alıcısı) ödünç verir. **Kalıcı hiçbir şey yazılmaz** — yalnızca `--live`:
misafiri kapatmak her zaman tam bir geri almadır.

Misafire **ikinci bir disk** vermek için (sistem diski değil):

```sh
vfioctl guest --name win11-nvme nvme --attach 0000:02:00.0   # domain kapalıyken
vfioctl guest --name win11-nvme nvme --detach 0000:02:00.0
```

Denetleyiciyi **bütün olarak** verir; misafir diski kendi kuyrukları ve kendi
NVMe ad alanıyla görür, bölümlemeyi misafir yapar. Bağlı, `fstab`'da duran ya da
takas alan bir diski **reddeder** ve bunun bayrağı yoktur.

`--detach` yalnız bu aracın bıraktığı satırı siler. **Sistem diskini mutlak
reddeder** — kip 2 ile kurulmuş bir denetleyici domain'den ancak domain'le
birlikte ayrılır (`clean`).

### Geri alma

```sh
vfioctl guest --name win11-nvme passthrough --off   # kartı domain'den çıkar
vfioctl guest --name win11-nvme clean               # domain + çalışma dizini sil
vfioctl uninstall                                   # host tarafını geri al
```

`clean` **fiziksel diske dokunmaz** — kip 2 ile kurulmuş bir diski kimliğiyle
raporlar ve bırakır. Üstünde çalışan bir Windows kalan diski daha sonra geri
almak için:

```sh
vfioctl guest --name win11-nvme build --adopt --system-nvme 0000:02:00.0
```

Domain'i diskin etrafında **tanımlar, kurmaz**: diske hiç yazmaz, ortam takmaz,
misafiri başlatmaz. Ölçüldü (2026-08-19): sahiplenilen domain **12 saniyede**
açıldı, taze NVRAM'e rağmen. → [--adopt](docs/TASARIM.md#--adopt--fiil-değil-bayrak)

`uninstall` dosyaları siler, ACL satırını geri alır, `driver_override`'ı
temizler — ama kartı nvidia'ya **canlı döndürmez** (o yol makineyi kilitliyor;
reboot bedavaya yapıyor). Kart `vfio-pci`'deyken reddeder. Yedekler (`.bak`)
silinmez, listelenir.

---

## Komutlar

| Komut | Ne yapar | Yazar mı |
|---|---|---|
| `doctor` | makineyi ölçer, kapıyı açar/kapatır | hayır |
| `profiles` | tanınan makineleri listeler | hayır |
| `inventory` | devredilebilir cihazlar + host'a bedeli | hayır |
| `install [--check]` | host tarafının dokuz parçası | `/etc` |
| `uninstall` | kurulanı geri alır | `/etc` |
| `selftest` | N tur gerçek devir, yargılanmış | hayır (günlük) |
| `guest build` | boş diskten kurulu Windows'a | domain + imaj |
| `guest build --adopt` | kurulu bir diski sahiplenir | yalnız domain |
| `guest setup` | misafir betiklerini sürer (NVIDIA/VDD/LG) | misafir |
| `guest passthrough` | kartı domain'e verir/alır | domain tanımı |
| `guest nvme` | NVMe denetleyicisi verir/alır | domain tanımı |
| `guest usb` | koşan misafire aygıt ödünç verir | hayır (canlı) |
| `guest eject` | kurulum ortamını sürücüden çıkarır | domain tanımı |
| `guest autologon` | konsol oturumunu açar | misafir kaydı |
| `guest status` / `screenshot` | durum / ekran görüntüsü | hayır |
| `guest clean` | domain + çalışma dizini siler | siler |

Her komutun kendi yardımı var: `vfioctl <komut> --help`,
`vfioctl guest <komut> --help`.

---

## Bilinen sorunlar

**USB Bluetooth radyosu misafirde Kod 10 veriyor.** Radyo misafire devredilince
doğru adıyla görünüyor ama başlamıyor (`CM_PROB_FAILED_START` / `0xC0000001`);
host tarafında `btusb` hiç düşmüyor. Çözüm domain tanımına tek satır —
`<qemu:del capability='usb-host.hostdevice'/>` — ve **vfioctl bunu yazmaz**,
elle yazılır.

Düzeltme ampirik ve tekrarlanabilir, **kök sebebi bilinmiyor**: beş açıklama
ölçülüp elendi, kaynakta doğrulanmış ama cihazda ölçülmemiş bir aday var. Her
USB devri bunu istemiyor — aynı makinede bir fare alıcısı varsayılan kipte
sorunsuz devredildi. Ayrıntı, ölçümler, elenen açıklamalar ve bilinen risk
(satırın bir gün **sessizce** etkisizleşme kipi dahil) →
[docs/bluetooth-code10.md](docs/bluetooth-code10.md).

## Yol haritası

| Faz | İçerik |
|---|---|
| 0 | taşınma + iskelet ✅ |
| 1 | kapı: donanım profili biçimi, `doctor`, seans yarısının ölçümü ✅ |
| 2 | host kurulumu: devir hook'u, udev kuralları, Looking Glass host yarısı ✅ |
| 3 | misafir inşası + misafir betiklerini süren kod ✅ |
| 4 | envanter ✅, oturuma bağlı USB devri ✅, disk devri ✅ |

Dört fazın dördü de kapandı. Faz 4'ün iki yarısı birbirinin küçük kardeşi değil,
ayrı iki mekanizma: **USB devri** koşan misafire ödünç verir ve hiçbir yere
yazmaz; **disk devri** kalıcı tanıma aittir ve sert korumanın muhatabı odur.
Sistem diskini fiziksel NVMe yapan kurulum kipi ve onun geri yönü (`--adopt`)
2026-08-19'da uçtan uca ölçüldü.

## Sırlar

Kişisel hiçbir değer bu depoya girmez: hesap adı, parola, yerel ayar ve ISO yolu
çalışma anında verilir. Üretilen `autounattend.xml` ve yardımcı ISO
`~/.images/<domain>-unattend/` altında 0700 dizinde, makine-yerel kalır. Depoya
giren **şablon**dur.

## Lisans

[MIT](LICENSE). Kullanın, değiştirin, dağıtın; telif satırını koruyun.

**Garanti yoktur:** bu araç `/etc`'e yazar ve bir grafik oturumunu bozabilir —
önce `doctor`, sonra `install --check`.
