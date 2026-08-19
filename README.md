# vfioctl

🇹🇷 Bir dizüstünün harici ekran kartını (dGPU) Windows misafirine devreden
VFIO passthrough kurulumunu kuran, ölçen ve süren CLI aracı.
🇬🇧 CLI tool that installs, checks and drives a VFIO passthrough setup which
hands a laptop's discrete GPU to a Windows guest.

> **Durum: yol uçtan uca çalışıyor, bu makinede ölçülmüş hâliyle.** Boş
> diskten, kartı devredilmiş ve Looking Glass'ın o kart üzerinde yakaladığı bir
> Windows misafirine kadar her adım burada: **donanım kapısı** (`doctor`),
> **host kurulumu** (`install` / `uninstall` / `selftest`) ve **misafir inşası
> + sürülmesi** (`guest/`). Kalan: ek cihaz devri (Faz 4) — envanteri çıkaran
> yarısı yazıldı (`inventory`), **devreden yarısı henüz yok.**

## Kurulum

```sh
git clone https://github.com/drpars/vfioctl && cd vfioctl && makepkg -si
```

`makepkg` `base-devel` ister; `depends` yalnızca `python` ve `libvirt`, geri
kalan her şey `optdepends` — çünkü araç eksik olanı ölçüp komutu basar, kurmaz.

Ağaç tek yere (`/usr/lib/vfioctl`) kurulur ve `/usr/bin/vfioctl` ona
symlink'tir; ikisi de zorunlu, gerekçesi [PKGBUILD](PKGBUILD)'in başlığında.
Kurulduktan sonra komut `vfioctl`; aşağıdaki örnekler klondan koşmayı gösterdiği
için `./vfioctl` yazar, ikisi aynı şeydir.

**Otomatik güncelleme kanalı yoktur.** Araç AUR'da değil, yani `pacman -Syu`
onu tazelemez; güncelleme `git pull && makepkg -si`. Bu bilinçli bir bedel —
paket olarak yayımlamak, aracın vaat vermediği bir taşınabilirliği ima ederdi
(→ [Kapsam](#kapsam--ve-kapsamda-olmayan)). Düzeltmelerin bir kısmı zaten kod
değil **belge** olarak yolculuk ediyor (→ [Bilinen sorunlar](#bilinen-sorunlar)),
onlar da aynı yolla ulaşır: depoya yeniden bakarak.

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
- Kartı devretmeyi mümkün kılan hiçbir alt komut profil eşleşmesi olmadan
  koşmaz — `install`, `selftest`, `guest passthrough`, artı fiziksel bir diske
  kuran `build --system-nvme`. `--force` yoktur: yarı çalışan bir passthrough
  kurulumu, hiç kurulmamış olmaktan kötüdür.
- Kapı yazan **her** komutta reddetmez: kurulanı **geri alan** yollar bilerek
  geçer — `uninstall` sorar, kapalıysa söyler ve yine de koşar; `guest clean`
  hiç sormaz. Yoksa sınıftan düşmüş bir makinede dosyalar mahsur kalırdı.
  Host'un hiçbir şeyini hareket ettirmeyen `build --disk` de sormaz; sınıf dışı
  makinede koşar, yalnızca Looking Glass'ı olmaz. Sorusunun daha sert sahibi
  olan yollar (`guest nvme --attach`, `guest usb`) `inventory`'nin hükmüne
  cevap verir.

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
vfioctl                   # TEK giriş noktası: doctor, inventory, profiles,
│                         # install, uninstall, selftest + guest <alt komut>
core/                     # probe (makineyi okur) + profile + doctor/gate
│                         # + session (seans yarısını ölçer, yazmaz)
│                         # + inventory (başka ne devredilebilir, bedeli ne)
│                         # + hostfiles/install (host tarafı) + selftest
data/50-vfio-handover     # devri yapan libvirt hook'u
docs/                     # tek bir arızanın kaydı: ölçüm, elenenler, risk
profiles/                 # tanınan makineler, birer .toml
guest/
├── build.py              # boş diskten konsol oturumu açık Windows'a, gözetimsiz
│                         # (kendi başına koşulmaz -- `vfioctl guest`)
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

**Sert ölçütün üç hâli var, ikisi değil.** Ölçülemeyen bir sert ölçüt kapıyı
kapatmaz — MUX özniteliği sürücüsüyle gelir ve modül yüklü değilken dosya hiç
doğmaz; onu "geçmedi" saymak, donanımı hiç değişmemiş makinede aracı durdurmak
olurdu. Ama "geçti" de sayılmaz: rapor bu ölçütleri kendi başlığı altında
sayar, ve kapı açıkken bile *"ölçülemeyen var"* der.

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

### Envanter — başka ne devredilebilir, ve bedeli ne

```sh
./vfioctl inventory       # PCI + USB, her cihazın host'a bedeliyle
```

**Yalnızca rapordur, hiçbir şey uygulamaz** — uygulayan iki komut aşağıdaki
`guest usb` ile `guest nvme`, ve ikisi de hükmü buradan sorar. Cevapladığı soru "yapılabilir mi"
değil — neredeyse her cihaz teknik olarak ayrılabilir —
**"host ne kaybeder"**. `doctor`'dan ayrı bir komut olması bilerek: `doctor`'ın
tek bir hükmü var ve otuz satırlık bir döküm onu gömer, üstelik envanter tam da
kapı **kapalıyken** okunmaya değer, çünkü listelediği şey sebebidir.

**Devir birimi veri yoluna göre değişir, ve ikisini karıştırmak pahalıdır.**
Bir PCI cihazı bütün IOMMU grubuyla taşınır — grubunda yabancı varsa hiç
taşınamaz. Bir USB cihazı tek başına, `vendor:product` ile taşınır; ayırmayı
libvirt yapar. Dolayısıyla üstündeki her şeyi götüren şey *denetleyicidir*,
cihaz değil: bu makinede Bluetooth radyosu ile dizüstünün kendi klavyesi aynı
xHCI'da duruyor, yani radyoyu devretmek ucuz, denetleyicisini devretmek klavye
demek.

Ret ile uyarı ayrımı sabit: **ret veriye, ekrana ve geri alabilmeye dairdir** —
host'un bağlı olduğu, `fstab`'ında duran ya da takas aldığı bir disk; host'un
ekranını taşıyan kart; ve host'un **son** girdi aygıtı. Kaybolan bir klavye ya
da radyo misafiri kapatınca geri gelir, ama misafiri kapatmak da bir girdi
aygıtı ister — o yüzden sonuncusu reddedilir, geri kalanı yüksek sesle söylenip
geçilir.

**Envanter "bu cihazı hangi misafir istiyor"u da söyler.** İki domain aynı
kartı kalıcı tanımında tutabilir ve bunu bugüne kadar hiçbir yer söylemiyordu.
Satır iki biçimde çıkar: `misafir tanımında` kalıcı tanımdan okunur ve domain
kapalıyken de durur; `şu an misafirde` yalnız koşan bir misafirin canlı olarak
aldığı aygıtlardır (`guest usb`'nin ödüncü — misafir kapanınca geri gelir).
**Hüküm bundan etkilenmez:** iddia, hükümler verildikten *sonra* eklenir, yani
bir misafirin cihazı istemesi devri ne engeller ne serbest bırakır. libvirt
sorulamıyorsa (kurulu değil, ayakta değil, izin yok) envanter yine tam çalışır
ve **başlığında sorulamadığını yazar** — boş bir satır "kimse istemiyor"
demek değildir.

**İşaretin kuralı tektir ve iki veri yolunda da aynıdır: bedel varsa `!`, yoksa
`✓`.** Bir zamanlar iki kez yazılmıştı ve iki kopya birbirini tutmuyordu —
makinedeki tek Bluetooth radyosu, denetleyicisinin satırında bedel sayılıp
kendi satırında bedelsiz `✓` alıyordu. **`✓` "ölçülen bedel yok" demektir,
"bedelsiz" değil:** ölçülen şey dört sorudur, o yüzden bağlı olmayan bir disk
de kartın kendisi de `✓` alır (aynı ayrım disk tarafında da yazılı, bkz.
"Kip 2"). **Ve yalnız `✗` engeldir** — `!` uyarır, devri durdurmaz; hükmü okuyan
iki komut da (`guest usb`, `guest nvme`) yalnızca `✗`'e bakar.

### Koşan misafire USB aygıtı ödünç vermek

```sh
./vfioctl guest --name win11 usb                        # kimde ne var
./vfioctl guest --name win11 usb --attach 8087:0032     # ödünç ver
./vfioctl guest --name win11 usb --detach 8087:0032     # geri al
```

Kart misafire geçtikten **sonra**, misafir koşarken bir USB aygıtını (Bluetooth
radyosu, oyun kolu, bellek, fare alıcısı) o oturuma ödünç verir.

**Kalıcı hiçbir şey yazılmaz.** Yalnızca `--live`, asla `--config`: domain'in
saklı tanımı hiç değişmez, ödünç verilen aygıt misafir kapanınca libvirt
tarafından host'a geri verilir. Yani **misafiri kapatmak her zaman tam bir geri
alma**dır; girdi aygıtı kuralının tek başına yetmesini sağlayan da bu.

**Devir hook'u bu yola hiç karışmaz.** Hook `type='pci'` süzer, USB hostdev'i
hiç görmez (ölçüldü: yalnız USB taşıyan bir XML'e `no handover` diyor); zaten
hook'lar domain başlarken koşar, canlı takmada koşmazlar. Kartı taşıyan tek
yazarın hook olduğu ilkesi bu komutla bozulmuyor — burada ayırmayı libvirt'in
kendisi yapıyor, ve belgesi bunu USB için zaten üstleniyor (`managed`
özniteliği **yalnızca PCI** için okunur).

**Neden `passthrough`'un bir bayrağı değil:** `passthrough` **kapalı** bir
domain'in saklı tanımını düzenler ve kartı başlangıçta hook taşır; bu komut
**koşan** bir misafire takar ve hiçbir iz bırakmaz. Tek komutta toplamak, bir
bayrağın "domain kapalı olmalı" ile "domain koşuyor olmalı" arasında karar
vermesi demek olurdu.

Kanıt iddia değil ölçümdür: komut takmadan önce ve sonra **host tarafındaki
sürücüleri** okur (`btusb` gitti mi), domain'in canlı XML'ini okur, ve ajan
varsa **misafirin kendi aygıt envanterini** sorar (`Get-PnpDevice`). Aynı
`vendor:product`'tan iki tane takılıysa devir yapılmaz — libvirt hangisini
alacağını ayırt edemez.

### Bütün bir NVMe denetleyicisini misafire vermek

```sh
./vfioctl guest --name win11 nvme                          # domain'de hangisi var
./vfioctl guest --name win11 nvme --attach 0000:02:00.0     # ver
./vfioctl guest --name win11 nvme --detach 0000:02:00.0     # geri al
```

Denetleyiciyi **bütün olarak** verir: misafir diski kendi kuyrukları ve kendi
NVMe ad alanıyla görür, host onu domain koştuğu sürece hiç görmez; bölümlemeyi
misafir yapar.

**Bu komut misafire ikinci bir disk verir.** Sistem diskini fiziksel bir
denetleyici yapmak ayrı bir yoldur ve `build`'in bayrağıdır → "İki kurulum
kipi". Denetleyici bir kez oraya yazıldıysa bu komut onu **çıkarmaz**: sistem
diski domain'den ancak domain'le birlikte ayrılır (`clean`).

**Cevap dosyası ISO'su hâlâ takılıysa `--attach` sorar.** ISO'yu `build` takar
ve hiçbir komut geri çıkarmaz; autounattend `DiskID 0`'ı `WillWipeDisk` ile
siler ve şablonun kendi gerekçesi *"DiskID 0 domain'deki tek disktir"* —
ikinci bir fiziksel disk tam da o varsayımı kaldırır. Sıranın ne olacağı bu
makinede **ölçülmedi**, o yüzden komut reddetmiyor: durumu söylüyor ve
`build --system-nvme`'nin kullandığı EVET onayını istiyor (terminalsiz koşu
için `--confirm-unattended`).

**`--detach` yalnız bu aracın bıraktığı satırı siler.** `managed='no'` kart
bloğunu reddeder — onu devir hook'u bağlar (K8). `managed='yes'` bir bloğu ise
ancak iki işaretten biri varsa siler: kimlik kaydı o adresi adlandırıyorsa, ya
da adres hâlâ bir NVMe denetleyicisi taşıyorsa. Adreste hiç cihaz yoksa da
siler (bayat satır tam olarak budur). Geriye kalan tek hâl — kaydı yok **ve**
adreste başka bir canlı cihaz var, ör. başka bir aracın taktığı ağ kartı —
reddedilir ve `virsh edit`'e yollar.

**Hep ya hiç, ve bunu donanım söylüyor.** PCI devri bütün IOMMU grubunu taşır,
denetleyicinin yarısı diye bir şey yok. Diskin bir kısmı host'ta kalacaksa cevap
bu komut değil: denetleyici yerinde bırakılır ve misafire ham bölüm verilir
(virtio-blk).

**Red K14'ün sert korumasıdır ve bayrağı yoktur.** Bağlı, `fstab`'da duran ya da
takas alan bir disk devredilmez; hükmü bu komut türetmez, `inventory`'den sorar —
tek sahip, çünkü ikinci bir tablo sessizce kayan tablo olur. Ölçüldü: boot diski
(`0000:05:00.0`) gerekçesiyle birlikte reddediliyor, XML'e dokunulmadan.

**`managed='yes'` — kartın tersi, ve aynı ilkeden.** Kartın zaten bir yazarı var:
devir hook'u, nvidia yığınını belirli bir sırayla boşaltmak zorunda. Oraya
libvirt'i de sokmak aynı sysfs yollarına ikinci bir yazar koymak olurdu, ve bu
makineyi üç kez kilitleyen şey tam olarak buydu — o yüzden kart `managed='no'`.
Diskin böyle bir yazarı yok ve gerekmiyor: `nvme` unbind'da bırakıyor, probe'da
geri alıyor. Yani disk için **tek yazar libvirt'tir**, ve `managed='yes'` onu tek
yazar yapan şeydir. Hook etkilenmiyor (ölçüldü: disk eklenmiş XML'de
`configured:` yalnız kartın iki işlevi, `hostdev:` üçü — karar değişmiyor).

**Boot'ta kalıcı bağlama BİLEREK yapılmadı.** `modprobe.d ids=` ya da udev
`driver_override` diski her açılışta `vfio-pci`'ye çivilerdi, ama anahtarı
`vendor:device` olurdu — o bir **model** adıdır, sürücünün kendisi değil. Boot
diski aynı modelden olan bir makinede kural boot diskini kapar, üstelik bu
aracın korumaları koşamadan; belirtisi açılmayan makinedir. K14'ün koruması
ancak host'un bağlarını ve `fstab`'ını okuyabildiği yerde dürüst olabilir, o yer
de burasıdır — erken boot değil.

**Neden `passthrough`'un bir bayrağı değil:** ikisi de kapalı domain'i düzenler,
ama `passthrough` kartı verir ve kartı hook taşır; bu komut depolama verir ve
onu libvirt taşır. Tek bayrağa indirmek, iki farklı taşıyıcıyı tek adın altına
saklamak olurdu.

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
onunla birlikte ölür. Çıktı `$XDG_STATE_HOME/vfioctl/selftest.log`'a da yazılır
(öntanımlı `~/.local/state/vfioctl/selftest.log`), sonuç bu yüzden okuyucudan
sağ çıkar. `/tmp`'de değil: kurtarması reboot olan arıza günlüğü de siliyordu.

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
./vfioctl guest build --user <ad> --password-file <yol>
./vfioctl guest setup          # (kart varsa NVIDIA) → VDD → Looking Glass → tek ekran
./vfioctl guest passthrough    # domain'e kartı ver (geri almak: --off)
./vfioctl guest nvme --attach 0000:02:00.0   # domain'e NVMe denetleyicisi ver
./vfioctl guest setup --start  # domain'i başlat + turu koş (kartlıysa: düz VT)
./vfioctl guest status
./vfioctl guest clean
```

**Misafir tarafı ayrı bir çalıştırılabilir değil, bir ad alanı.** İki yarının
ön koşulları ayrı: yukarısı `sudo` ile `/etc`'ye yazar ve hiçbir misafire
dokunmaz, `guest` altı libvirt'e ve çalışan bir Windows'a konuşur, root
istemez. Tek düz listeye dökmek bu ayrımı yardım sayfasında görünmez kılardı;
ayrı çalıştırılabilir bırakmak ise ikinci bir ad demekti — bayatlayan hep o
olur, çünkü onu kimse sınamaz. `vfioctl guest --help` kendi yardımını basar.

Bu makinede ölçüldü: boş diskten ajanı ulaşılabilir, konsol oturumu açık bir
Windows 11 Pro 25H2'ye **7 dk 33 sn**, elle adım yok.

#### İki kurulum kipi — ve ikisi birbirini dışlar

```sh
./vfioctl guest --name win11-b build --disk ~/.images/win11-b.qcow2 …   # kip 1
./vfioctl guest --name win11-b build --system-nvme 0000:02:00.0 …        # kip 2
```

Gerisi birebir aynı: `passthrough`, `setup`, `status`, `clean` iki kipte de aynı
komutlar. **Değişen tek şey `build`'in satırı** — yeni bir fiil yok, çünkü
domain tanımlayan ikinci bir kod yolu ikinci bir cevap demek olurdu.

**Bayrak `--disk`'in yerine geçer, yanına değil.** İkisi birden verilirse
reddedilir: bir domain'in bir sistem diski olur, ve `clean`'in iki hedefi olmaz.
`--size` de kip 2'de reddedilir — o kip imaj üretmiyor.

**Ölçülen ve ölçülmeyen.** Ölçülen: libvirt `<boot order>`'ı
`<hostdev managed='yes'>` içinde kabul ediyor ve geri okumada aynı yerde
duruyor; domain'in `<metadata>`'sı devredilen denetleyicinin kimliğini
(`model`, `serial`, `ids`) `role="system"` ile birlikte aynen koruyor.
**Ölçülmeyen: OVMF'in o denetleyiciden gerçekten önyükleyebildiği, ve Windows
Setup'ın diski `list disk`'te kaçıncı sırada gördüğü.** İkisi de gerçek bir boot
istiyor ve o tur koşulmadı — bu kip bugün **sınanmamış** bir yoldur.

**Kurulum diski siler, ve son kapı insandır.** Cevap dosyası `DiskID 0`'ı baştan
bölümlüyor. Envanterin `✓`'i *"host onun üstünde durmuyor"* demektir, **"boş"
demek değil** — `inventory` diskin içeriğini hiç okumaz, yani veri dolu ama
bağlı olmayan bir disk de `✓` alır. O yüzden kip 2 diski **model + seri** ile
yazar ve `EVET` yazılmasını bekler; `--confirm-wipe` ile geçilir, ve **tty
yoksa soru sorulamadığı için tur reddedilir** (sorulacak kimse olmaması onay
değildir). Bayrak bilerek `--yes` değil: `setup`'ın `--yes`'i *"kartı
masaüstünün içinden devret"* demek ve `build --setup` aynı yolu sürüyor —
tek bayrak iki onayı taşırdı.

**`clean` fiziksel diske dokunmaz.** Domain'i ve çalışma dizinini kaldırır,
sonra sistem diskinin **kimliğiyle** orada durduğunu ve silinmemesinin bilerek
olduğunu söyler. Silme komutu **basılmaz**: `wipefs` yalnız imzayı, `blkdiscard`
bütün namespace'i, `nvme format` LBA boyutunu da değiştirebilir — üçü aynı şey
değil ve hiçbiri bu makinede ölçülmedi. Ölçülmemiş reçete yazılmaz.

**Sistem diski domain'den ancak domain'le birlikte ayrılır.** `nvme --detach`
onu **mutlak** reddeder ve bunun bayrağı yoktur: izin verilseydi geriye
açılmayan bir domain ve hiçbir komutun geri koyamayacağı bir disk kalırdı, çünkü
boot sırasını yalnız `build --system-nvme` yazar ve o da diski baştan bölümlüyor.
Çıkış yolu `clean`'dir — diski içeriğine dokunmadan serbest bırakır.

`setup`, `guest/windows/` altındaki betikleri misafire iter ve sırayla koşar.
Sıra bir bağımlılık: ekran topolojisinin yalıtacak bir şeyi olması için önce
VDD'nin ekranı doğmalı, VDD'nin render edeceği kartın da adını taşıyabilmesi
için önce sürücüsü kurulmalı. **Sürücü adımı yalnızca domain kartı alıyorken
koşar** — kartsız provada kurulacak bir şey yok. Bu yüzden her adım **kurucunun çıkış kodunu değil
misafirin envanterini** okur — VDD'nin oluşturduğu monitör, LG servisinin
durumu, topolojinin kendi hükmü. VDD'nin render edeceği bağdaştırıcı da
misafirde keşfedilir (`--gpu-name` ile geçilebilir): adı yalnızca Windows
bilir, ve yanlış ad "LG bağlanıyor ama hiç kare gelmiyor" olarak görünür. Turun
sonunda bu iddia da ölçülür: Looking Glass host'un kendi günlüğünden **hangi
bağdaştırıcıda yakaladığı** okunur, ve VDD'ye verilenle uyuşmuyorsa tur düşer.

**Looking Glass'ın iki yarısı aynı release olmak zorunda** — ayrıysa istemci
paylaşımlı belleği reddeder ve arıza "görüntü hiç gelmiyor" diye görünür, yani
çalışmayan bir devirden ayırt edilemez. Bu yüzden sürüm üç yerden okunur, hiçbiri
elle yazılmaz: ana makinede **kurulu istemcinin** sürümü (PATH'teki ikili
çözülür, sahibi pakete sorulur), depoda `guest/windows/looking-glass.ps1`'in
pin'i (`$Version`/`$Url`/`$Sha256` — üçü birlikte), ve misafirde **koşan** host
uygulamasının kendi günlüğü. `doctor` ilk ikisini karşılaştırır (misafir
gerekmez; yumuşak uyarı — devri değil görüntüyü engelleyen bir fark), `setup`
uyuşmazlıkta LG adımını **hiç koşmaz** ve indirme adresini istemcinin sürümünden
türetip basar, `status` misafirdekiyle istemcidekini yan yana yazar. Sürümü
okunamayan bir makinede cevap "bilinmiyor"dur ve uyuşmazlık sayılmaz.

**Kart domain'e ayrı bir adımda girer (`passthrough`).** Kartsız domain,
masaüstü canlıyken istenildiği kadar sürülebilir — hata ayıklama turu kimseden
bir şey götürmez. Kart girdiği anda tur bir **devir** turu olur: `setup --start`
domain'i kendisi başlatır, kartın hangi sürücüde bittiğini sysfs'ten okur ve
her şeyi `/tmp/vfioctl-setup.log`'a yazar — `selftest` gibi, düz bir VT'den
koşulur. İki tur **tek bir şeyle** ayrılır; sonucu okunabilir kılan da budur.

Komut yalnızca **kapalı** bir domain'i düzenler ve `managed='no'` yazar:
çalışan bir misafire hostdev takmak da, libvirt'e cihazı kendi çözdürmek de
karta ikinci bir yazar demek olurdu. Kartı yalnızca hook taşır.

**Yıkıcı yol yalnızca bu betiğin ürettiği misafirlere uygulanır.** `build`
tanımladığı her domain'i kendi ad alanında işaretler; `clean` işaretsiz bir
domain'e ve başka bir domain'in diskine dokunmaz. Ad listesi tutulmuyor — o,
yalnızca yazıldığı makineyi korur.

**Her dosyanın başlığında gerekçeli bir açıklama var.** Neyin neden öyle
yazıldığı — hangi API'nin sessizce başarısız olduğu, hangi sıranın zorunlu
olduğu — koddan değerli; oralar okunmadan değiştirilmemeli.

## Bilinen sorunlar

**USB Bluetooth radyosu misafirde Kod 10 veriyor.** Radyo misafire devredilince
doğru adıyla görünüyor ama başlamıyor (`CM_PROB_FAILED_START` / `0xC0000001`);
host tarafında `btusb` hiç düşmüyor. Çözüm domain tanımına tek satır —
`<qemu:del capability='usb-host.hostdevice'/>` — ve **vfioctl bunu yazmaz,**
elle yazılır. Düzeltme ampirik ve tekrarlanabilir, **kök sebebi bilinmiyor** —
beş açıklama ölçülüp elendi, ve kaynakta doğrulanmış ama cihazda ölçülmemiş bir
aday var. Her USB devri bunu istemiyor: aynı makinede bir fare alıcısı
varsayılan kipte sorunsuz devredildi. Ayrıntı, ölçümler, elenen açıklamalar,
sebep adayı ve bilinen risk (satırın bir gün **sessizce** etkisizleşme kipi
dahil) → [docs/bluetooth-code10.md](docs/bluetooth-code10.md).

## Yol haritası

| Faz | İçerik |
|---|---|
| 0 | taşınma + iskelet ✅ |
| 1 | kapı: donanım profili biçimi, `doctor`, seans yarısının ölçümü ✅ |
| 2 | host kurulumu: devir hook'u, udev kuralları, Looking Glass host yarısı ✅ |
| 3 | misafir inşasının kalan yarısı: misafir betiklerini süren kod ✅ |
| 4 | envanter ✅, oturuma bağlı USB devri ✅, disk devri ✅ |

Faz 4'ün iki yarısı birbirinin küçük kardeşi değil, ayrı iki mekanizma.
**Oturuma bağlı USB devri** koşan misafire ödünç verir, hiçbir yere yazmaz,
misafir kapanınca geri alınır. **Disk devri** ise kalıcı tanıma ait ve K14'ün sert
korumasının muhatabı o; misafir denetleyiciyi boş bir disk olarak görür.
Reddetme yolu envanterde yazılı ve ölçülü. Sistem diskini fiziksel NVMe yapan
kurulum kipi (`build --system-nvme`) yazıldı; **gerçek bir boot ile
ölçülmedi** → "İki kurulum kipi".

Faz 3'ün kabul ölçütü bu makinede karşılandı: kartsız prova ve kartlı tur geçti
(LG host günlüğü kartı adıyla yazıp `Capture Start` dedi, VDD ekranı 2560x1440),
ardından çalışan misafirde `selftest` yine 5/5 temiz çıktı.

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

[MIT](LICENSE). Kullanın, değiştirin, dağıtın; telif satırını koruyun.
**Garanti yoktur:** bu araç `/etc`'e yazar ve bir grafik oturumunu bozabilir —
önce `doctor`, sonra `install --check`.
