# vfioctl — tasarım gerekçeleri

Bu belge, [README](../README.md)'nin *"neden böyle"* yarısıdır: hangi kararın
neyi önlediği, hangi yolun ölçülüp elendiği, hangi bayrağın neden var olmadığı.

README'den ayrıldı çünkü iki farklı okuyucusu var. **README'yi kurulum yapmak
için okursunuz** — sıra, komut, tuzak. **Burayı bir şeyi değiştirmeden önce
okursunuz** — çünkü buradaki her paragraf, kaldırıldığında geri gelen bir
arızanın kaydı.

Ölçümlerin ham dökümü bu depoda değil, araştırma klasöründedir (`pars/vfioctl/`:
`NOTLAR.md` + aylık arşiv). Buraya giren şey **karar** ve **gerekçesi**.

---

## Neden ayrı bir proje

Passthrough kurulumu iki yarıya bölünmüştü: host yapılandırması bir Arch
kurulum aracının içinde, misafir tarafı bir not klasöründe. Bölünme, kurulumun
ikinci bir makinede koşmasını yapısal olarak imkânsız kılıyordu — dört
yapılandırma dosyası için baştan sona bir kurulum aracı koşmak gerekiyordu.

Ayıran test şu: *bu dosya neden var?* Tek gerekçesi passthrough olan her şey
buraya gelir; passthrough olmasa da doğru olanlar (libvirt paketleri, `default`
ağı, grup üyelikleri) archsetup'ta kalır.

---

## Kapsam — kapı, vaat değil

Hedef sınıfı: dGPU + iGPU taşıyan, MUX'lu, dGPU'sunun IOMMU grubu izole olan
ASUS dizüstüler. Bunun bir **vaat** değil bir **kapı** olması tasarımın parçası:

- `doctor` her makinede koşar, hiçbir şey yazmaz, neyin uymadığını tek tek
  söyler — başka bir makineye taşımak isteyenin ihtiyacı budur.
- Kartı devretmeyi mümkün kılan hiçbir alt komut profil eşleşmesi olmadan
  koşmaz: `install`, `selftest`, `guest passthrough`, artı fiziksel bir diske
  kuran `build --system-nvme`.
- **`--force` yoktur.** Yarı çalışan bir passthrough kurulumu, hiç kurulmamış
  olmaktan kötüdür.

Kapsam dışı: tek GPU'lu makineler (host ekranını kaybeden **başka** bir
tasarım), AMD dGPU'ların reset bug'ı.

### Kapının bilerek reddetmediği yollar

Kapı yazan **her** komutta reddetmez; kurulanı **geri alan** yollar bilerek
geçer:

- `uninstall` sorar, kapalıysa söyler ve **yine de koşar**; `guest clean` ile
  `guest eject` hiç sormaz. Yoksa sınıftan düşmüş bir makinede dosyalar, ve bir
  domain'e bırakılmış kurulum ortamı, mahsur kalırdı.
- Host'un hiçbir şeyini hareket ettirmeyen `build --disk` de sormaz: sınıf dışı
  makinede koşar, yalnızca Looking Glass'ı olmaz.
- Sorusunun daha sert sahibi olan yollar (`guest nvme --attach`, `guest usb`)
  `inventory`'nin hükmüne cevap verir.
- **`build --adopt` de bu son kümede:** diske yazmaz, yalnız var olan bir
  sistemin etrafına domain tanımlar — `nvme --attach`'in yazdığı satırı artı bir
  boot girdisi. Ek fren, geri alınamayan yol için alındı; sahiplenme turunu
  `clean` bütünüyle geri alır.

### Seans yarısı: yazılmaz, ölçülür

Kurulumun taşıyıcı koşullarından biri, **grafik oturumunun dGPU'ya hiç
dokunmaması.** Ölçüt tam olarak şu: devir anında oturumun hiçbir süreci dGPU'nun
DRM düğümünü (`/dev/dri/card*`) ya da `/dev/nvidia*`'ı açık tutmamalı — ve
sabitleme compositor **başlamadan önce** kurulmuş olmalı, çünkü cihaz seçimi
süreç başlarken bir kez okunur.

Bu yarıyı vfioctl **yazmaz**: sahibi kullanıcının kendi masaüstü
yapılandırmasıdır (bu makinede ayrı bir dotfiles deposu). Ama **ölçer** —
`doctor` her makinede bakar, ve geçmiyorsa ölçütü ve nereye kurulacağını,
**neyin ölçülmediğini yazarak** basar.

Ölçülmüş olan: Hyprland (`AQ_DRM_DEVICES`), bu makinede. Ölçülmemiş olan: KWin,
mutter ve diğerleri — KWin'in kendi DRM cihaz seçici değişkeni var
(`KWIN_DRM_DEVICES`), bu araç onu denemedi. Buna karşılık kurulumun iki parçası
compositor'den **bağımsız**: dGPU'nun DRM düğümünü seat envanterinden çıkaran
udev kuralı **logind** üzerinden çalışır (mekanizma compositor'ün değil), ve
seans değişkenlerinin dördünün üçü glvnd + Vulkan loader'ına aittir, her
masaüstünde aynıdır.

Duruş bu yüzden "yalnızca Hyprland çalışır" değil: **ölçüt söylenir, ölçüm
kullanıcının makinesinde `doctor` ile yapılır.** Başka bir compositor'de
çalışacağı vaat edilmiyor; çalışıp çalışmadığını o makinede okumanın yolu var.

---

## `doctor` — sert ölçütün üç hâli var, iki değil

Ölçülemeyen bir sert ölçüt kapıyı **kapatmaz**: MUX özniteliği sürücüsüyle gelir
ve modül yüklü değilken dosya hiç doğmaz; onu "geçmedi" saymak, donanımı hiç
değişmemiş makinede aracı durdurmak olurdu. Ama "geçti" de sayılmaz — rapor bu
ölçütleri kendi başlığı altında sayar, ve kapı açıkken bile *"ölçülemeyen var"*
der.

**Seans denetimleri kapıyı etkilemez** ve ölçülemediklerinde "geçmedi" değil
**"ölçülemedi"** derler: kapı makineye bakar (kalıcı soru), seans denetimi ana
bakar (her boot değişir). Compositor denetimi kapıya konsaydı kapı kendi kendini
kilitlerdi — `selftest` düz VT'den koşulur, orada ölçülecek compositor yoktur.

**`doctor`'ın fd taraması yetkisizdir ve bunu artık söyler.** *"dGPU'yu tutan
bir şey görünmüyor"* satırı, göremediği süreçlerin **sayısını** da basar: root'un
süreçleri (ör. `/dev/nvidia0` üstünde on bir fd tutan `nvidia-powerd`), ve
setuid/setgid ikilileri — uid'in tutması süreci okunabilir yapmaz. Ölçümü
genişletmek mümkün değil: yetkisiz okunabilen ikinci kaynak yok
(`/sys/kernel/debug/dri/` root'a kapalı, `/proc/driver/nvidia/gpus/*/` istemci
listelemiyor). O yüzden doğru hamle **iddiayı ölçüsüne indirmek** oldu; ölçümü
büyütmek değil. Güvenlik açığı değil: devir hook'u `nvidia-powerd`'i adıyla
tanır ve `unload_nvidia` onu kendisi durdurur.

---

## `inventory` — devir birimi, ret/uyarı, işaret kuralı

**Yalnızca rapordur, hiçbir şey uygulamaz** — uygulayan iki komut `guest usb`
ile `guest nvme`, ve ikisi de hükmü buradan sorar. Cevapladığı soru "yapılabilir
mi" değil (neredeyse her cihaz teknik olarak ayrılabilir), **"host ne
kaybeder"**.

`doctor`'dan ayrı bir komut olması bilerek: `doctor`'ın tek bir hükmü var ve
otuz satırlık bir döküm onu gömer, üstelik envanter tam da kapı **kapalıyken**
okunmaya değer — çünkü listelediği şey sebebidir.

**Devir birimi veri yoluna göre değişir, ve ikisini karıştırmak pahalıdır.** Bir
PCI cihazı bütün IOMMU grubuyla taşınır; grubunda yabancı varsa hiç taşınamaz.
Bir USB cihazı tek başına, `vendor:product` ile taşınır ve ayırmayı libvirt
yapar. Dolayısıyla üstündeki her şeyi götüren şey **denetleyicidir**, cihaz
değil: bu makinede Bluetooth radyosu ile dizüstünün kendi klavyesi aynı xHCI'da
duruyor — radyoyu devretmek ucuz, denetleyicisini devretmek klavye demek.

**Ret ile uyarı ayrımı sabit: ret veriye, ekrana ve geri alabilmeye dairdir.**
Host'un bağlı olduğu, `fstab`'ında duran ya da takas aldığı bir disk; host'un
ekranını taşıyan kart; ve host'un **son** girdi aygıtı. Kaybolan bir klavye ya
da radyo misafiri kapatınca geri gelir — ama misafiri kapatmak da bir girdi
aygıtı ister, o yüzden sonuncusu reddedilir, geri kalanı yüksek sesle söylenip
geçilir.

**"Bu cihazı hangi misafir istiyor" da söylenir.** İki domain aynı kartı kalıcı
tanımında tutabilir ve bunu bugüne kadar hiçbir yer söylemiyordu. Satır iki
biçimde çıkar: `misafir tanımında` kalıcı tanımdan okunur ve domain kapalıyken
de durur; `şu an misafirde` yalnız koşan bir misafirin canlı olarak aldığı
aygıtlardır. **Hüküm bundan etkilenmez** — iddia, hükümler verildikten *sonra*
eklenir; bir misafirin cihazı istemesi devri ne engeller ne serbest bırakır.
libvirt sorulamıyorsa envanter yine tam çalışır ve **başlığında sorulamadığını
yazar**: boş bir satır "kimse istemiyor" demek değildir.

**İşaretin kuralı tektir ve iki veri yolunda da aynıdır: bedel varsa `!`, yoksa
`✓`.** Bir zamanlar iki kez yazılmıştı ve iki kopya birbirini tutmuyordu —
makinedeki tek Bluetooth radyosu, denetleyicisinin satırında bedel sayılıp kendi
satırında bedelsiz `✓` alıyordu.

> **`✓` "ölçülen bedel yok" demektir, "bedelsiz" değil.** Ölçülen şey dört
> sorudur; disk **içeriği hiç okunmaz** (`blkid`/`wipefs`/`FSTYPE` yok). Veri
> dolu ama bağlı olmayan bir disk de `✓` alır. Kip 2'nin insan onayı tam da bu
> yüzden var.

**Ve yalnız `✗` engeldir** — `!` uyarır, devri durdurmaz; hükmü okuyan iki komut
da yalnızca `✗`'e bakar.

---

## `install` — dört şey bilerek yapılmıyor

- **Paket kurulmaz.** Looking Glass'ın iki yarısı AUR'da; `install` onları ölçer
  ve yoksa komutu basıp durur. Olmayan bir modül için yapılandırma yazmak,
  sessiz başarısızlığın ders kitabı hâlidir. Ölçüm **paket adına değil ürüne**
  bakar (`modinfo kvmfr`, `looking-glass-client` var mı) — dağıtımdan bağımsız.
- **Karta dokunulmaz.** Ne `install` ne `selftest` bir PCI cihazını bağlar,
  çözer ya da probe eder. Devri libvirt hook'u yapar — **tek yazar**, bilerek.
- **`qemu.conf`'un `user`/`group` satırları yazılmaz**, yalnızca raporlanır:
  onların gerekçesi passthrough değil. vfioctl o dosyada **yalnızca**
  `cgroup_device_acl` satırına dokunur. İki düzenleme de satır kapsamlı, dosyayı
  yeniden yazan yok — bu yüzden birbirini ezmiyorlar.
- **Seans yarısı yazılmaz**, ölçülür ve **uyarı** olarak basılır. Reddedilmez:
  compositor kartı tutuyorsa `install` tam da bunun çaresidir — seat kuralı daha
  bir dakika önce diskte yoktu.

**Sudo modeli: `sudo tee`.** Araç root koşmaz, yetki yazma anında alınır.
Gerekçe: root şart koşulursa `doctor` da root altında koşma alışkanlığı doğar —
oysa yazmaması gereken tek komut odur.

**Kalıcı yapılandırma PCI adresi yazmaz.** Her iki udev kuralı da
`ATTRS{vendor}`/`ATTRS{device}` ile eşleşir. Sebep ölçüldü: bir disk takası
iGPU'yu taşıyınca sabit adrese bağlı kural ateşlemedi, symlink oluşmadı ve
compositor dGPU'yu tuttu — ve **üç hafta hiçbir şey hata vermedi**, çünkü hiç VM
başlatılmadı.

**`install --check` kalıcı bir araçtır**, tek seferlik bir doğrulama değil:
`/etc` kayar, paket güncellemesi dosya geri koyar, biri gecenin üçünde VT'den
kural düzenler. Sorusu şu — *bu makine hâlâ kurduğumuz şey mi?*

> **⚠ Hükmü KURULU koda göredir.** Klondan koşan bir doğrulama, makinede
> yürürlükte olanı ölçmez: bir kez `/etc` doğruydu ve fark görünmedi, oysa
> kurulu paket düzeltmeyi hiç taşımıyordu ve PATH'teki `vfioctl install` iki
> kuralı eski hâline **geri yazardı**. Aracın sürümü denetimin parçasıdır.

**Yedek silinmez, listelenir.** Bir `.bak`, o yola vfioctl ilk kez yazmadan önce
orada ne varsa onun kopyasıdır — başka bir aracın kurduğu makinede geriye kalan
tek kopya odur, ve ait olduğu dosya az önce silinmiştir.

**`uninstall` modüle dokunmaz:** dosyaları siler, ACL satırını geri alır,
`driver_override`'ı temizler — ama kartı nvidia'ya **canlı döndürmez**. O yol
makineyi üç kez kilitledi; reboot bedavaya yapıyor. Kart `vfio-pci`'deyken
**reddeder** (misafir sahibi olabilir).

---

## `selftest` — yoklayıcı testin parçasıdır

Kartı saniyede bir açan kısa ömürlü bir süreç (bu makinede waybar'ın `gputemp`
modülü) devri deterministik olmaktan çıkarıp **zar atışına** çeviriyordu;
düzeltmenin işe yarayıp yaramadığı ancak öyle bir süreç canlıyken ölçülebilir.

Bu yüzden `selftest` yoklayıcı yoksa ya da durdurulmuşsa **koşmayı reddeder** ve
her turda onun cpu deltasını basar — sıfır delta turu "sonuç geçersiz" yapar.
`--no-poller` bilerek sessiz taban çizgisi içindir, **kabul turu değildir**.

**Günlük `$XDG_STATE_HOME/vfioctl/selftest.log`'a yazılır, `/tmp`'ye değil:**
kurtarması reboot olan arıza, günlüğü de siliyordu — ve bir kez gerçekten sildi.

**Ad `selftest`, `handover` değil** — komut kartı hiç ellemiyor, fiilî devri
hook yapıyor. "handover" adı bir gün gerçekten devreden bir kip ima eder, ve
ikinci bir yazar tam olarak makineyi üç kez kilitleyen şeydi.

**`selftest` seans yarısının ölçüldüğü yer değildir.** Preflight'ı "compositor
kartı şu an tutuyor mu" diye bakar ve tutuyorsa turu hiç başlatmaz; ama neyin
gerektiğini ve nereye kurulacağını söyleyen komut `doctor`. `selftest`'in sorusu
daha dar ve daha pahalı: gerçek bir devir, gerçek bir misafirle, art arda beş
kez.

---

## `guest usb` — neden `passthrough`'un bir bayrağı değil

`passthrough` **kapalı** bir domain'in saklı tanımını düzenler ve kartı
başlangıçta hook taşır; `usb` **koşan** bir misafire takar ve hiçbir iz
bırakmaz. Tek komutta toplamak, bir bayrağın *"domain kapalı olmalı"* ile
*"domain koşuyor olmalı"* arasında karar vermesi demek olurdu.

**Kalıcı hiçbir şey yazılmaz.** Yalnızca `--live`, asla `--config`: domain'in
saklı tanımı hiç değişmez, ödünç verilen aygıt misafir kapanınca libvirt
tarafından host'a geri verilir. Yani **misafiri kapatmak her zaman tam bir geri
almadır**; girdi aygıtı kuralının tek başına yetmesini sağlayan da bu.

**Devir hook'u bu yola hiç karışmaz.** Hook `type='pci'` süzer, USB hostdev'i
hiç görmez (ölçüldü: yalnız USB taşıyan bir XML'e `no handover` diyor); zaten
hook'lar domain başlarken koşar, canlı takmada koşmazlar. Kartı taşıyan tek
yazarın hook olduğu ilkesi bozulmuyor — burada ayırmayı libvirt'in kendisi
yapıyor, ve belgesi bunu USB için zaten üstleniyor (`managed` özniteliği
**yalnızca PCI** için okunur).

**Kanıt iddia değil ölçümdür:** komut takmadan önce ve sonra host tarafındaki
sürücüleri okur (`btusb` gitti mi), domain'in canlı XML'ini okur, ve ajan varsa
**misafirin kendi aygıt envanterini** sorar (`Get-PnpDevice`). Aynı
`vendor:product`'tan iki tane takılıysa devir yapılmaz — libvirt hangisini
alacağını ayırt edemez.

---

## `guest eject` — neden bir fiil gerekti

`build` cevap dosyası ISO'sunu takıyor ve kendisi çıkarmıyordu. Ölçüldü: bir
domain, kurulumu bittikten çok sonra hâlâ taşıyordu ve aynı domain'de Windows
ISO'su **boot order 1**'deydi. İkisi birlikte, sonraki açılışta gözetimsiz
Setup'ı yeniden koşturabilecek bir tanım demek — ve cevap dosyası `DiskID 0`'ı
siliyor.

**Ortamı çıkarır, sürücüyü değil.** `<disk device='cdrom'>` yerinde kalır, yalnız
`<source>` gider — libvirt'te "eject" budur; sürücüyü silmek altındaki SATA
birimlerini yeniden numaralar ve kurulmuş bir misafir sürücü harflerini
hatırlar. Boşaltılan sürücü `boot order` taşıyorsa bu **söylenir**: firmware
sıradaki girdiye geçer.

**Hiçbir dosya silinmez** — ISO'lar yerinde durur; silme `clean`'in işidir. Bu
yüzden komutun kendi onayı yok: geri takmak `virsh edit` ile tek satır.
Varsayılan yalnız cevap dosyasını çıkarır, çünkü oraya onu bu araç koydu ve tek
başına bir açılışı yıkıcı yapan disk odur; kurulum ISO'su ile virtio-win
kullanıcının seçimi (onarım açılışında işe yarar) ve ancak `--all` ile giderler.

---

## `guest nvme` — tek yazar, hep ya hiç, K14

**Hep ya hiç, ve bunu donanım söylüyor.** PCI devri bütün IOMMU grubunu taşır;
denetleyicinin yarısı diye bir şey yok. Diskin bir kısmı host'ta kalacaksa cevap
bu komut değil: denetleyici yerinde bırakılır ve misafire ham bölüm verilir
(virtio-blk).

**Red K14'ün sert korumasıdır ve bayrağı yoktur.** Bağlı, `fstab`'da duran ya da
takas alan bir disk devredilmez; hükmü bu komut türetmez, `inventory`'den sorar
— **tek sahip**, çünkü ikinci bir tablo sessizce kayan tablo olur.

**`--detach` yalnız bu aracın bıraktığı satırı siler.** `managed='no'` kart
bloğunu reddeder — onu devir hook'u bağlar. `managed='yes'` bir bloğu ise ancak
iki işaretten biri varsa siler: kimlik kaydı o adresi adlandırıyorsa, ya da
adres hâlâ bir NVMe denetleyicisi taşıyorsa. Adreste hiç cihaz yoksa da siler
(bayat satır tam olarak budur). Geriye kalan tek hâl — kaydı yok **ve** adreste
başka bir canlı cihaz var, ör. başka bir aracın taktığı ağ kartı — reddedilir ve
`virsh edit`'e yollar.

**Neden `passthrough`'un bir bayrağı değil:** ikisi de kapalı domain'i düzenler,
ama `passthrough` kartı verir ve kartı **hook** taşır; bu komut depolama verir
ve onu **libvirt** taşır. Tek bayrağa indirmek, iki farklı taşıyıcıyı tek adın
altına saklamak olurdu.

### `managed='yes'` — kartın tersi, ve aynı ilkeden

Kartın zaten bir yazarı var: devir hook'u, nvidia yığınını belirli bir sırayla
boşaltmak zorunda. Oraya libvirt'i de sokmak aynı sysfs yollarına ikinci bir
yazar koymak olurdu — ve bu makineyi üç kez kilitleyen şey tam olarak buydu. O
yüzden kart `managed='no'`.

Diskin böyle bir yazarı yok ve gerekmiyor: `nvme` unbind'da bırakıyor, probe'da
geri alıyor. Yani disk için **tek yazar libvirt'tir**, ve `managed='yes'` onu
tek yazar yapan şeydir. Aynı tek-yazar ilkesi, ters yönde.

> **⚠ libvirt yolu çekirdek günlüğünde sessiz değildir.** Gidişte libvirt ayrıca
> **PCI reset** atar (`vfio-pci 0000:02:00.0: resetting` / `reset done`),
> dönüşte tam bir `nvme` probe'u basılır (7 satır) ve o satırlar **açılıştakiyle
> birebir aynıdır**. Hata değil, normal probe — karşılaştırılmazsa arıza sanılır.

### Boot'ta kalıcı bağlama BİLEREK yapılmadı

`modprobe.d ids=` ya da udev `driver_override` diski her açılışta `vfio-pci`'ye
çivilerdi, ama anahtarı `vendor:device` olurdu — o bir **model** adıdır,
sürücünün kendisi değil. Boot diski aynı modelden olan bir makinede kural boot
diskini kapar, üstelik bu aracın korumaları koşamadan; belirtisi **açılmayan
makinedir**.

K14'ün koruması ancak host'un bağlarını ve `fstab`'ını okuyabildiği yerde dürüst
olabilir — o yer de burasıdır, erken boot değil.

### Elenen yol: NVMe'yi `vfio.conf`'a alıp hook'a bağlatmak

Kapı "herhangi biri", eylem **"hepsi"** — `$DEVICES` bir menü değil **bir IOMMU
grubu**. Kartı isteyen misafir diski de alırdı, diski isteyen kartı da. Deneyle
de kapandı (hook'un `VFIO_HOOK_CHECK` kipi). Aynı ölçüm bir sınıfı kapatıyor:
*"şu cihazı da `vfio.conf`'a ekleyelim"* önerisi, cihaz kartın grubunda değilse
aynı yerde düşer.

---

## `build` — iki kurulum kipi, ve ikisi birbirini dışlar

Kip 1 `--disk ~/.images/x.qcow2` der, kip 2 `--system-nvme 0000:02:00.0`.
**Değişen tek şey `build`'in satırıdır** — `passthrough`, `setup`, `status`,
`clean` iki kipte de birebir aynı komutlar. Yeni bir fiil yok, çünkü domain
tanımlayan ikinci bir kod yolu ikinci bir cevap demek olurdu.

**Bayrak `--disk`'in yerine geçer, yanına değil.** İkisi birden verilirse
reddedilir: bir domain'in bir sistem diski olur, ve `clean`'in iki hedefi olmaz.
`--size` de kip 2'de reddedilir — o kip imaj üretmiyor.

**Bu komut ile `guest nvme` farklı sorulara cevap verir.** `nvme --attach`
misafire **ikinci bir disk** verir; sistem diskini fiziksel bir denetleyici
yapmak `build`'in bayrağıdır. Denetleyici bir kez sistem diski olarak
yazıldıysa `nvme --detach` onu **mutlak** reddeder ve bunun bayrağı yoktur: izin
verilseydi geriye açılmayan bir domain ve hiçbir komutun geri koyamayacağı bir
disk kalırdı, çünkü boot sırasını yalnız `build --system-nvme` yazar ve o da
diski baştan bölümlüyor. Çıkış yolu `clean`'dir — diski **içeriğine dokunmadan**
serbest bırakır.

### Kurulum diski siler, ve son kapı insandır

Cevap dosyası `DiskID 0`'ı baştan bölümlüyor, ve envanterin `✓`'i *"host onun
üstünde durmuyor"* demek — **"boş" demek değil**. O yüzden kip 2 diski **model +
seri** ile yazar ve `EVET` yazılmasını bekler; `--confirm-wipe` ile geçilir, ve
**tty yoksa soru sorulamadığı için tur reddedilir** (sorulacak kimse olmaması
onay değildir).

Bayrak bilerek `--yes` değil: `setup`'ın `--yes`'i *"kartı masaüstünün içinden
devret"* demek ve `build --setup` aynı yolu sürüyor — tek bayrak iki onayı
taşırdı, ve **yazılmadığı hâlde verilen** onay, kimsenin fark etmediği onaydır.

### Ölçülen ve ölçülmeyen (kip 2)

Ölçülen: libvirt `<boot order>`'ı `<hostdev managed='yes'>` içinde kabul ediyor
ve geri okumada aynı yerde duruyor; domain'in `<metadata>`'sı devredilen
denetleyicinin kimliğini (`model`, `serial`, `ids`) `role="system"` ile birlikte
aynen koruyor.

2026-08-19'da gerçek bir boot ile ölçülenler: OVMF namespace'i **önyükleme adayı
olarak listeliyor** (`UEFI <model> <seri> 1`, üstünde ESP olmadığı hâlde), ve
Windows Setup'ın gördüğü **tek** disk `Disk 0` — yani cevap dosyasının hedefi
devredilen denetleyicidir. Kurulumun kendisi de ölçüldü: gözetimsiz tur
devredilen denetleyiciye **8 dk 8 sn**'de tamamlandı, misafir o diski `Disk 0`
olarak `IsBoot`+`IsSystem` ile dört bölümlü GPT üstünde gösteriyor, ve kurulum
ortamı çıkarıldıktan sonra **hiç CD yokken 10 sn**'de oradan açılıyor.

Aynı turda ölçülen ikinci şey: kip 2'de **üç virtio sürücüsünden ikisi**
gerekiyor — `NetKVM` (ağ) ve `vioserial` (ajanın kanalı) bir cihaza bağlanıyor,
`viostor` sürücü deposunda kalıyor ve hiçbir cihaz talep etmiyor, çünkü diski
Windows'un kutu içi `stornvme`'i sürüyor.

> **⚠ İki taraf FARKLI seri alanı gösterir, ve bu disk karışıklığı değildir.**
> Misafirin `SerialNumber`'ı namespace **EUI-64**'üdür; host'un `by-id`/`wwid`'i
> **denetleyici serisini** kullanır, çünkü Linux EUI-64'ü *"Ignoring bogus
> Namespace Identifiers"* deyip atar ve sysfs'te `eui`/`nguid` dosyaları hiç
> doğmaz. İkisini karşılaştıran bir tur "yanlış disk devredilmiş" sanır;
> kimliğin ölçütü **model + boyut + `vendor:device`**.

### `--adopt` — fiil değil bayrak

`clean` diski silmediği için ortaya bir nesne çıkıyor: **üstünde çalışan bir
Windows olan, domain'i olmayan fiziksel disk.** Onu geri alacak yarı iki komuta
bölünmüştü ve ikisinde de eksikti — `build` yalnız silerek kuruyor, `nvme
--attach` boot sırasını yazmıyor. `--adopt` o eksik yarı: domain'i diskin
etrafında tanımlar, **kurmaz**.

**Gerekçesi ölçülü değil yapısal:** domain tanımlayan ikinci bir kod yolu ikinci
bir cevap olurdu, ve ayrışan taraf, bir domain'i yanlış tanımlayana kadar
kimsenin okumadığı taraf olur. Böylece şablon, `<metadata>` kaydı
(`role="system"`), K14 kapısı ve `guard()` olduğu gibi kullanılır. `--adopt`
yalnız `--system-nvme` ile geçerlidir: kip 1'de `clean` qcow2'yi zaten siliyor,
geriye sahiplenilecek bir şey kalmıyor.

**Silme yolu atlanmıyor, erişilemez oluyor** — bayrağın bağlayıcı tasarım ölçütü
buydu. Sahiplenme turu cevap dosyasını **üretmiyor**, yardımcı ISO'yu
**kurmuyor**, domain'e **hiçbir ortam takmıyor**, sonra **tanımı geri okuyup**
yüklü tek bir CD-ROM'da bile turu reddediyor, ve misafiri **hiç başlatmıyor**.
Koşula bağlı olarak atlanan bir silme, koşulun yanlış hesaplandığı gün
gerçekleşen bir silmedir.

**Önyüklenebilirlik sınanmaz.** Ölçmek diskin içeriğini okumayı gerektirir, oysa
bu araç hiçbir yerde okumaz — `inventory`'nin `✓`'i "boş" demediği gibi bu
bayrağın sessizliği de "açılır" demez. Tur domain'i tanımlar ve bunu söyler;
cevabı ilk başlatma verir ve o komut kullanıcınındır.

**O komut bir kez koşuldu, ve cevap evet** (2026-08-19): kip 2 ile kurulmuş bir
disk `clean` ile serbest bırakıldı, `--adopt` ile yeniden tanımlandı ve **12
saniyede açıldı**. Bu, aracın hükmünü değiştirmez — turun sessizliği hâlâ bir
vaat değil — ama bir tuzağı kapatıyor: `clean` domain'i `undefine --nvram` ile
kaldırdığı için sahiplenen domain **taze NVRAM**'le doğar, yani Windows'un
firmware'e yazdığı önyükleme girdisi orada yoktur. Ölçülen: bu engel değil, yol
kendini onarıyor.

**Bu tur `EVET` sormaz, çünkü silecek bir şeyi yok** — ama `--confirm-wipe` ile
birlikte verilirse **reddeder**: silme onayı, silmeyen bir turda sessizce kabul
edilmez. `--setup` de reddedilir; bu tur misafiri başlatmıyor.

---

## `setup` — sıra bir bağımlılıktır

`setup`, `guest/windows/` altındaki betikleri misafire iter ve sırayla koşar.
Sıra keyfi değil: ekran topolojisinin yalıtacak bir şeyi olması için önce
VDD'nin ekranı doğmalı, VDD'nin render edeceği kartın da adını taşıyabilmesi
için önce sürücüsü kurulmalı. **Sürücü adımı yalnızca domain kartı alıyorken
koşar** — kartsız provada kurulacak bir şey yok.

Bu yüzden her adım **kurucunun çıkış kodunu değil misafirin envanterini** okur:
VDD'nin oluşturduğu monitör, LG servisinin durumu, topolojinin kendi hükmü.
VDD'nin render edeceği bağdaştırıcı da misafirde keşfedilir (`--gpu-name` ile
geçilebilir): adı yalnızca Windows bilir, ve yanlış ad *"LG bağlanıyor ama hiç
kare gelmiyor"* olarak görünür. Turun sonunda bu iddia da ölçülür — Looking
Glass host'un kendi günlüğünden **hangi bağdaştırıcıda yakaladığı** okunur, ve
VDD'ye verilenle uyuşmuyorsa tur düşer.

> **⚠ Bir hükmü bağımsız doğrulamak, başka bir aletle ölçmek değildir.** Aletin
> o kanaldan doğru cevap verdiği de bilinmelidir:
> `[System.Windows.Forms.Screen]::AllScreens` misafirde `WinDisc 1024x768`
> dedi — ne ad ne çözünürlük tutuyordu. Sebep: **`guest-exec` oturum 0'da
> koşar, orada ekran aygıtı yoktur.** Doğru alet
> `Get-CimInstance Win32_VideoController`.

### Looking Glass'ın iki yarısı aynı release olmak zorunda

Ayrıysa istemci paylaşımlı belleği reddeder ve arıza *"görüntü hiç gelmiyor"*
diye görünür — yani çalışmayan bir devirden **ayırt edilemez**.

Bu yüzden sürüm üç yerden okunur, hiçbiri elle yazılmaz: ana makinede **kurulu
istemcinin** sürümü (PATH'teki ikili çözülür, sahibi pakete sorulur), depoda
`guest/windows/looking-glass.ps1`'in pin'i (`$Version`/`$Url`/`$Sha256` — üçü
birlikte), ve misafirde **koşan** host uygulamasının kendi günlüğü.

`doctor` ilk ikisini karşılaştırır (misafir gerekmez; yumuşak uyarı — devri
değil görüntüyü engelleyen bir fark), `setup` uyuşmazlıkta LG adımını **hiç
koşmaz** ve indirme adresini istemcinin sürümünden türetip basar, `status`
misafirdekiyle istemcidekini yan yana yazar. Sürümü okunamayan bir makinede
cevap "bilinmiyor"dur ve uyuşmazlık sayılmaz.

---

## `passthrough` — kartı yalnızca hook taşır

Komut yalnızca **kapalı** bir domain'i düzenler ve `managed='no'` yazar: çalışan
bir misafire hostdev takmak da, libvirt'e cihazı kendi çözdürmek de karta ikinci
bir yazar demek olurdu.

**Kart domain'e ayrı bir adımda girer, çünkü ayrımı korumaya değer.** Kartsız
domain, masaüstü canlıyken istenildiği kadar sürülebilir — hata ayıklama turu
kimseden bir şey götürmez. Kart girdiği anda tur bir **devir** turu olur ve düz
VT'den koşulur. İki tur **tek bir şeyle** ayrılır; sonucu okunabilir kılan da
budur.

**Kart `managed='no'` olduğu için "başladı + `vfio-pci`" tek başına makbuzdur:**
host sürücüsü kartı tutuyor olsaydı domain hiç başlamazdı.

---

## `clean` — yıkıcı yol yalnız kendi misafirlerine

`build` tanımladığı her domain'i kendi ad alanında işaretler; `clean` işaretsiz
bir domain'e ve başka bir domain'in diskine dokunmaz. **Ad listesi tutulmuyor** —
o, yalnızca yazıldığı makineyi korur.

**`clean` fiziksel diske dokunmaz.** Domain'i ve çalışma dizinini kaldırır, sonra
sistem diskinin **kimliğiyle** orada durduğunu ve silinmemesinin bilerek
olduğunu söyler. Silme komutu **basılmaz**: `wipefs` yalnız imzayı, `blkdiscard`
bütün namespace'i, `nvme format` LBA boyutunu da değiştirebilir — üçü aynı şey
değil ve hiçbiri bu makinede ölçülmedi. **Ölçülmemiş reçete yazılmaz.**

---

## Misafir tarafı neden ayrı bir çalıştırılabilir değil

`guest` bir ad alanıdır, ikinci bir program değil. İki yarının ön koşulları
ayrı: üst düzey komutlar `sudo` ile `/etc`'ye yazar ve hiçbir misafire dokunmaz,
`guest` altı libvirt'e ve çalışan bir Windows'a konuşur, root istemez.

Tek düz listeye dökmek bu ayrımı yardım sayfasında görünmez kılardı; ayrı
çalıştırılabilir bırakmak ise ikinci bir ad demekti — **bayatlayan hep o olur**,
çünkü onu kimse sınamaz.

---

## Kaynak kodun kendisi

**Her dosyanın başlığında gerekçeli bir açıklama var.** Neyin neden öyle
yazıldığı — hangi API'nin sessizce başarısız olduğu, hangi sıranın zorunlu
olduğu — koddan değerli; oralar okunmadan değiştirilmemeli.
