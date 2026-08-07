# USB Bluetooth radyosu misafirde Kod 10 veriyor

🇬🇧 **Summary.** A USB Bluetooth radio handed to a Windows guest enumerates
under its correct name but fails to start (Code 10 / `CM_PROB_FAILED_START`,
`0xC0000001`). Removing the `usb-host.hostdevice` QEMU capability from the
domain makes it work: libvirt then addresses the device with
`hostbus=`/`hostaddr=` instead of a device-node path that QEMU opens and feeds
to `libusb_wrap_sys_device()`. **The workaround is empirical and reproducible;
the root cause is not known**, though a source-level candidate is described
below. Five explanations were measured and ruled out — they are listed so
nobody spends a session re-deriving them.

---

Bu dosya tek bir arızanın kaydıdır. vfioctl bu düzeltmeyi **uygulamaz** —
domain tanımına elle yazılır; aracın konumu ve gerekçesi → [aşağıda](#vfioctl-bunu-neden-yazmıyor).

## Semptom

USB Bluetooth radyosu koşan misafire ödünç verildiğinde (`vfioctl guest usb
--attach` ya da domain tanımında kalıcı bir `hostdev`):

- Misafir cihazı **doğru adıyla** görüyor — yani enumerate oluyor.
- Aygıt Yöneticisi'nde **Kod 10**, ayrıntısı `CM_PROB_FAILED_START` /
  `0xC0000001` (`STATUS_UNSUCCESSFUL`).
- Host tarafında `hci0` **duruyor**, arayüzün sürücüsü hâlâ `btusb`.

Semptom kaynaktan türetilen davranışla birebir: ep0 trafiği arayüz claim'i
gerektirmediği için cihaz görünür, ama arayüz I/O'su `-EBUSY` ile reddedilir ve
sürücünün başlatma yordamı düşer.

## Ölçüm ortamı

| | |
|---|---|
| Radyo | Intel `8087:0032`, USB `1-4`, xHCI `05:00.3` |
| Host | Arch Linux, `linux-g14` 7.1.4, libvirt 12.6.0, QEMU 11.0.3 |
| Misafir | Windows 11 Pro 25H2 (derleme 26200) |
| Makine | ASUS ROG Strix G513RM |

## Çözüm

Domain tanımına, domain düzeyinde:

```xml
<domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
  ...
  <qemu:capabilities>
    <qemu:del capability='usb-host.hostdevice'/>
  </qemu:capabilities>
</domain>
```

**İki yarı ya da hiçbiri:** `xmlns:qemu` namespace bildirimi yoksa libvirt
`qemu:` bloğunu **sessizce düşürür** — dosya kabul edilir, satır yok sayılır.

Üç şey bu satırın doğasından gelir ve ölçüldü:

- **Başlangıç anında etkilidir.** Koşan bir misafire canlı aygıt takmak bunu
  düzeltmez; satır domain başlarken okunur.
- **Cihaz adı taşımaz.** O domain'deki **her** USB `hostdev`'ini etkiler.
- **Cihazın kendisi kalıcı yazılmak zorunda değil.** Yeteneği kaldırılmış bir
  domain'e `--live` takmak da çalışıyor — yani "hiçbir USB aygıtı kalıcı
  yazılmaz" kuralı bozulmadan uygulanabilir, ve hiçbir aygıt takılı değilken
  satırın bedeli sıfır.

## Mekanizma — ölçülen kadarı

Yetenek, libvirt'in QEMU'ya cihazı **hangi adresleme kipiyle** verdiğini
belirliyor. QEMU her iki kipte de `usb_host_open()` içinde koşulsuz
`usb_host_detach_kernel()` çağırıyor, ama sonuç aynı olmuyor:

| Adresleme kipi | `1-4:1.0` sürücüsü | Host'ta `hci0` | Misafir |
|---|---|---|---|
| `hostdevice=/dev/bus/usb/BBB/DDD` (libvirt varsayılanı) | **`btusb`** — hiç düşmüyor | var | Kod 10 |
| `hostbus=`/`hostaddr=` (yetenek kaldırılınca) | **`usbfs`** | yok | **çalışıyor** |

**Ölçülecek doğru şey arayüzün sürücüsüdür**, modülün yüklü olup olmaması
değil:

```sh
cat /sys/bus/usb/devices/1-4:1.0/driver   # -> usbfs ise QEMU sahip
```

`modprobe -r btusb`'nin geçmesi bu soruyu cevaplamaz ve bir kez yanlış yola
soktu: libvirt aygıtı verdi, misafir onu adıyla gördü, `btusb` hiç düşmedi.

### Kip genel olarak bozuk değil

Bu bölümün erken bir sürümü *"fd yolunda sökme etkisiz kalıyor"* diyordu; fazla
güçlüymüş. Razer alıcısı (`1532:00c5`) **varsayılan fd kipinde** takıldı —
teslim kanıtı `qom-get`: `hostdevice=/dev/bus/usb/003/003`, `hostbus`/`hostaddr`
= 0 — ve `usb_host_detach_kernel()` **tuttu**: üç arayüz de `usbhid` → `usbfs`,
imleç misafirde çalıştı.

Yani etkisiz kalan kip değil, **o kipte o radyo**. Sonucu: bu düzeltme her USB
devrinin ön koşulu değil.

### Misafirdeki yığın gerçekten ayakta

PnP'nin `OK` demesi tek başına yeterli sayılmadı. Radyo + **Microsoft Bluetooth
Enumerator** + **RFCOMM Protocol TDI** + **LE Enumerator** hepsi `OK`, `bthserv`
koşuyor. Enumerator'lar ancak radyo fiilen çalışırken doğuyor.

## Sebep — aday cihazda doğrulandı (2026-08-07)

> **Bu bölümün başlığı "sebep bilinmiyor" idi; ölçüm yapıldı ve değişti.**
> Radyo, usbfs üzerinden gönderilen `GET_CONFIGURATION`'a **başarıyla `0x00`**
> cevaplıyor. Ölçüm ve sonuçları → aşağıdaki "Ayıracak ölçüm". Aşağıdaki beş
> eleme yürürlükte kalıyor; aday artık onların yanında değil, **üstünde**.

Olgu şuydu: `hostbus`/`hostaddr` çalışıyor, `hostdevice=` çalışmıyor. Aşağıdaki
beş açıklamanın **beşi de ölçülüp elendi**; yeniden türetilmesinler:

- **✗ Host sürücüsüyle çakışma.** `btusb` takmadan **önce** indirildi (arayüzler
  sürücüsüz, `hci` yok, QEMU cihazı sıfırdan açtı) → yine Kod 10.
- **✗ Başlatma sırasında cihazın yeniden enumerate olması.** Takma ile geri alma
  arasındaki 82 saniyede çekirdek günlüğünde **tek satır yok**, `devnum` 5'te
  sabit. Ne reset ne yeniden enumerate oldu.
- **✗ Firmware'in host tarafından zaten boot edilmiş olması.** `journalctl` hâlâ
  `Firmware already loaded` diyor, host o boot'ta radyoyu kullanmıştı, ve
  misafirin sürücüsü yine de başarıyla başlattı.
- **✗ `guestReset='off'`.** Teslim edildi (`qom-get guest-reset` → `false`),
  hüküm değişmedi.
- **✗ `suppress-remote-wake=false`.** Teslim edildi, hüküm değişmedi. Buradaki
  uyuşmazlık **gerçekti ama sebep değildi:** radyonun kendi descriptor'ı
  `bmAttributes = 0xe0` (Self-Powered **ve** Remote Wakeup — Microsoft'un
  Bluetooth için şart koştuğu iki bit), QEMU 5. biti varsayılan olarak siliyor.
  Bit geri verildiğinde de aygıt başlamadı. (Karşılaştırma: Razer `0xa0`,
  ASUS N-KEY klavye `0xe0`.)

**Denetleyicinin tamamını devretmek de çözmüyor:** Intel 3168 için xHCI'ın
kendisi devredildi ve yine Kod 10 + `STATUS_UNSUCCESSFUL` alındı. Arıza
denetleyicide değil.

### Bu düzeltme yaygın, ve kaynağı libvirt'in kendisi

Bu dosyanın erken bir sürümü *"dışarıda belgeli değil, öneren tek rapor unRAID"*
diyordu. Yanlışmış:

- **Reçeteyi libvirt bakımcısı vermiş.** libvirt GitLab
  [issue #99](https://gitlab.com/libvirt/libvirt/-/issues/99) (2020-11-23) aynı
  arıza sınıfı; bildiren kişi komut satırı deltasını birebir görmüş, Michal
  Prívozník yetenek-silmeyi orada önermiş.
- **Arızanın doğum commit'i belli:** libvirt
  [`bfb1ab1df1`](https://gitlab.com/libvirt/libvirt/-/commit/bfb1ab1df12e8dccfde42d1a6019bf2e628bf366)
  "qemu: Use .hostdevice attribute for usb-host" (2020-09-09, ilk 6.8.0).
  Gerekçesi cihazlarla ilgili değil: mount namespace + libusb önbelleği
  (rhbz 1595525, 1877218).
- **Yaygın:** `usb-host.hostdevice` dizesi GitHub kod aramasında 386 dosyada;
  ~15 bağımsız benimseyen (2021-08 → 2025-05), aralarında Intel'in kendi
  [`kvm-multios`](https://github.com/intel/kvm-multios) deposu (üç üretim
  Android misafir XML'inde).
- **Cihaz ailesi Intel'le sınırlı değil** (`8087:0aa7`, `0029`, `0026`, `0032`,
  AX211, artı bir Cambridge Silicon Radio dongle'ı), **ve misafir işletim
  sistemiyle ilgisi yok** — Linux misafirdeki yüzü `hci0: command 0xfc05 tx
  timeout` / `Reading Intel version command failed (-110)`.
- **İki kipin bilgi olarak denk olmadığı yukarı akışta kayıtlı.** QEMU
  [`202d69a715`](https://github.com/qemu/qemu/commit/202d69a715a4b1824dcd7ec1683d027ed2bae6d3)
  (2020-08-24, rhbz 1871090): *"libusb_get_device_speed() does not work for
  libusb_wrap_sys_device() devices"*, çözümü `USBDEVFS_GET_SPEED` ioctl'i ve
  **yalnızca fd yolunda** (`if (hostfd && libusb_speed == 0)`), bugünkü
  master'da hâlâ orada. Aynı sınıftan başka bir kusur, aynı kökten. **Emsal,
  açıklama değil.**

### Sebep adayı — kaynakta doğrulandı, ve cihazda ölçüldü

libusb'nin `op_wrap_sys_device()`'ı — `hostdevice=`'in vardığı yer —
`initialize_device(dev, busnum, devaddr, **NULL**, fd)` çağırıyor: `sysfs_dir`
NULL (libusb v1.0.30, `os/linux_usbfs.c`). Sonucu:

| | `hostbus/hostaddr` | `hostdevice=` |
|---|---|---|
| Aktif konfigürasyon nereden | sysfs `bConfigurationValue`, erken dönüş | **canlı `GET_CONFIGURATION` kontrol transferi** |
| Cihaz 0 cevaplar + config-0 yoksa | olamaz | `priv->active_config = -1` → `NOT_FOUND`, **kalıcı** |

`-1` oluşunca QEMU'da üç tüketici **sessizce** düşüyor: `usb_host_detach_kernel()`
erken dönüyor (→ host sürücüsü bağlı kalır), `usb_host_claim_interfaces()`
`LIBUSB_ERROR_NOT_FOUND`'u *"address state - ignore"* diye yutup **sıfır arayüz
claim ederek** `USB_RET_SUCCESS` dönüyor, `usb_host_ep_update()` erken dönüyor.
Misafirde `SET_CONFIGURATION` **başarılı** dönüyor, cihaz ep0 passthrough
sayesinde **doğru adıyla** görünüyor, ama her bulk/interrupt transferi
`default: USB_RET_STALL`'a düşüyor.

`-1` kalıcı, çünkü `usb_host_set_config()` `bNumConfigurations != 1` olmadıkça
`libusb_set_configuration()`'ı hiç çağırmıyor — onaracak tek yol koşmuyor.
**Ölçüldü: `8087:0032` için `bNumConfigurations = 1`.**

**Aday üç host tarafı ölçümün üçünü de tek bir başarısız çağrıdan türetiyor** ve
yukarıdaki beş elemenin hiçbirine dokunmuyor (host sürücüsü önceden indirilmiş
olsa da değişmemesi dahil). Kalan tek soru dardı — *bu radyo usbfs üzerinden
`GET_CONFIGURATION`'a `0` mu cevaplıyor?* — ve sysfs'in `bConfigurationValue=1`
demesi onu cevaplamıyordu, çünkü o çekirdeğin görüşü, teldeki cevap değil.
**2026-08-07'de telden ölçüldü: `0x00`, `status 0`** → yukarıdaki "Ayıracak
ölçüm". Aday artık hipotez değil.

**Bir düzeltme daha, aynı okumadan:** `usb_host_detach_kernel()`'in *çağrısı*
koşulsuz (iki kipte de düz hat üzerinde), ama içindeki `USBDEVFS_DISCONNECT`
ioctl'leri **iki kez korumalı** — descriptor çağrısı düşerse erken `return`, ve
arayüz başına `libusb_kernel_driver_active() != 1` ise `continue`. Bu erken
`return` adayın dayandığı kapı.

### Log'da ne var, ne yok

`/var/log/libvirt/qemu/<domain>.log` (QEMU stderr oraya düşüyor):

- **Var, ama ayırt edici DEĞİL:** `libusb_set_interface_alt_setting: -5
  [NOT_FOUND]`, tekrarlanıyor. Aynı log'daki `-device usb-host,…` başlatma
  satırlarıyla sıralandığında örneklerin **sonuncusu tek `hostbus=/hostaddr=`
  başlatmasından sonra** düşüyor — yani çalışan kipte de görülüyor. En olası
  okuma zararsız: radyonun arayüz 1'inde 7 alternate setting var (izokron SCO
  arayüzü) ve SCO alt setting'i iki kipte de tutmuyor. **Bu satır arızanın
  parmak izi değil.**
- **Yok, ve yokluğu YAPISAL — bir eşik meselesi değil:** `device unconfigured`
  ile `get configuration failed, errno=`, yani adayın öngördüğü libusb dizeleri.
  Sebebi şu: QEMU'nun `usb-host` aygıtındaki `loglevel` özelliği **ölü kod.**
  `static uint32_t loglevel` `usb_host_init()` içinde libusb'ye veriliyor, ama
  cihaz özelliği ondan **sonra** atanıyor ve `usb_host_init()` sonraki
  çağrılarda erken dönüyor — libusb'nin log seviyesi ömür boyu `NONE` kalıyor.
  Yani bu yol kalıcı olarak bilgi vermez; adayı log üzerinden onaylama/çürütme
  yolu **kapalı**, pozitif kontrol kurulamaz.
- **Yok, VE BU BİLGİ:** `libusb_detach_kernel_driver:` hata satırı. Farkı şu:
  QEMU'nun kendi hata yardımcısının log'a ulaştığı **biliniyor** — yukarıdaki
  `-5` satırları aynı biçimi kullanıyor. Yani detach **hiç hata bildirmedi**; ve
  host tarafında sürücünün bağlı kaldığı ölçülmüştü, yani **başarılı da olmadı.**

Kaynakta bu ikisini birlikte bırakan yalnızca iki yol vardı — **ve ikincisi
kaynaktan çürüdü, arıza tek mekanizmaya indi:**

1. descriptor kapısındaki erken `return` — yukarıdaki aday. **Ayakta.**
2. ~~`libusb_kernel_driver_active()`'ın 1 dönmemesi~~ — **ÇÜRÜK.**
   `op_kernel_driver_active()` (`os/linux_usbfs.c`) saf bir `USBDEVFS_GETDRIVER`
   ioctl'i: `sysfs_dir`'e, `priv->active_config`'e, cihazın nasıl yaratıldığına
   **hiç bakmıyor**. Çekirdek tarafı bağımsız olarak aynı yönde
   (`proc_getdriver()`, `devio.c`: yalnızca arayüze bağlı sürücüye bakar).
   fd'den wrap edilmiş olmak bu cevabı değiştiremez.

QEMU'daki sıra da bunu destekliyor: descriptor kapısı `kernel_driver_active`'ten
**önce** ve sessizce dönüyor — yani "detach hata bildirmedi ama başarılı da
olmadı" gözlemini aday **tek başına** açıklıyor.

### Ayıracak ölçüm — YAPILDI, ve cevap `0x00`

Aday, radyonun `GET_CONFIGURATION`'a **başarıyla `0x00`** cevaplamasını
gerektiriyordu. **2026-08-07'de ölçüldü ve tam olarak bu çıktı.**

Yol: `USBDEVFS_CONTROL` ioctl'i gönderen 45 satırlık bir C programı, libusb'nin
`usbfs_get_active_config()`'inin gönderdiği isteğin **birebir aynısı**
(`bmRequestType=0x80, bRequest=0x08, wValue=0, wIndex=0, wLength=1,
timeout=1000`). Claim yok, detach yok, `SET_CONFIGURATION` yok. Program
descriptor'dan `idVendor:idProduct`'ı doğrulayıp eşleşmezse çıkıyor, ve dönen
bayta nöbetçi değer (`0xAA`) koyuyor — böylece "cihaz `0x00` cevapladı" ile
"hiçbir şey yazılmadı" ayrılıyor. Root gerekiyor: `/dev/bus/usb/001/003`
`crw-rw-r-- root:root` ve **`uaccess` ACL'i taşımıyor** (aynı bustaki klavye
taşıyor — mekanizma çalışıyor, bu cihazı kapsamıyor).

**İki bağımsız tanık aynı şeyi söyledi.** Programın kendi çıktısı:

```
node=/dev/bus/usb/001/003 idVendor=8087 idProduct=0032 bNumConfigurations=1
ioctl=1 (aktarilan bayt) bConfigurationValue=0x00
```

ve `usbfs_snoop=Y` ile çekirdeğin kendi kaydı:

```
usb 1-4: control urb: bRequestType=80 bRequest=08 wValue=0000 wIndex=0000 wLength=0001
usb 1-4: ep0 ctrl-in, length 1, timeout 1000
usb 1-4: ep0 ctrl-in, actual_length 1, status 0
usb 1-4: data: 00
```

**`status 0` — stall değil, timeout değil, başarılı bir sıfır cevabı.** Aynı
anda çekirdeğin sysfs görüşü `bConfigurationValue = 1`, `bNumConfigurations = 1`.
Yani config-0 yok, ve `dev_has_config0()` sıfır dönecek → `priv->active_config`
**`-1`** olur. Adayın gerektirdiği her koşul cihazda sağlandı.

**Bedeli sıfır çıktı:** ölçümden sonra `hci0` ayakta, iki arayüz de `btusb`'de,
`rfkill` temiz, çekirdek günlüğünde tek hata satırı yok. Ölçüm bilerek en dar
patlama yarıçapında yapıldı — sıfır BT bağlantısı, iki VM de kapalı, klavye ve
fare USB (BT değil).

**`usbfs_snoop` tek başına ölçüm yolu değildi** ve bu da ölçüldü: o bir üretici
değil **tanık**. Transferi VM'siz üretecek hiçbir şey yok — normal libusb yolu
sysfs'ten okuyup erken dönüyor, çekirdek de kararlı durumda `GET_CONFIGURATION`
göndermiyor. Doğru kullanımı yukarıdaki gibi, programın **üstüne** bağımsız
ikinci tanık olarak.

## Bilinen risk — ve arıza kipinin adı var

`<qemu:capabilities>` libvirt'te bir *test* olanağı olarak belgeli
(`docs/drvqemu.rst`: *"meant for experiments only and should not be used in
production"*). Riskin somut şekli şu, ve gürültülü olan yarısı yanıltıcı:

- Bilinmeyen bir yetenek **adı** domain başlangıcında **sert hata** verir. Yani
  bir *yeniden adlandırma* gürültülü olurdu.
- **Ama libvirt bir yeteneği emekliye ayırırken adı silmez** — dizeyi bırakır,
  yalnızca C enum'unu `X_QEMU_CAPS_*` yapar. Emsali listede zaten duruyor:
  `"usb-host.bootindex", /* X_QEMU_CAPS_USB_HOST_BOOTINDEX */`.
- Emeklilikten sonra bu satır **hâlâ ayrıştırılır, domain hâlâ başlar, ve `del`
  sessiz bir no-op olur.** Kod 10 geri gelir, libvirt hiçbir yerde hata vermez.

Bu teorik değil: libvirt 2026-05-05'te asgari QEMU'yu 7.2'ye çekti, `hostdevice`
ise QEMU 5.1.0'dan (2020-06) beri var — yani bayrak artık her desteklenen
QEMU'da koşulsuz doğru ve ders kitabı emeklilik adayı. **Semptomu bilmek tek
savunma:** misafirde Kod 10 geri geldiğinde ilk bakılacak yer bu satırın hâlâ
etkili olup olmadığıdır (`virsh dumpxml` satırı gösterir ama etkili olduğunu
göstermez).

### Daha dar kapsamlı alternatif — değerlendirildi, elendi

`<qemu:override>` + `<qemu:property name='hostdevice' type='remove'/>` (libvirt
≥ 8.2.0) aynı komut satırını **tek cihaz için** üretir ve `<qemu:capabilities>`'in
aksine belgelidir. Ama uygulaması **yalnızca komut satırı yolunda**: USB hotplug
`qemuBuildUSBHostdevDevProps()` → `qemuMonitorAddDeviceProps()` diye gidiyor ve o
fonksiyona hiç uğramıyor. Yani **koşan misafire canlı takma özelliği kaybolur.**
Yetenek silme ise `priv->qemuCaps`'e bir kez işlendiği için domain ömrü boyunca
yaşıyor — canlı takmanın çalışmasının sebebi tam olarak bu.

### Yukarı akışa bildirmek — adres QEMU değil, libusb

Doğru uzun vadeli yol bu, ama sırası var ve **adresi ilk sanılan yer değil.**

**QEMU'ya bildirmek tek başına muhtemelen bekler:** `MAINTAINERS`'ta USB
bölümünün tamamı `S: Orphan`, `hw/usb/host-libusb.c` v11.0.3 ile master arasında
baytı baytına aynı, son işlevsel değişikliği 2021-07-29, ve bisect edilmiş commit
taşıyan bir issue 2025-09'dan beri cevapsız. İki sonucu: **QEMU'yu yükseltmek bu
davranışı değiştirmez**, ve rapor muhtemelen yamasını taşımak zorunda.

**Asıl adres libusb:** kusur master'da canlı (`op_wrap_sys_device()` hâlâ
`sysfs_dir`'i `NULL` geçiyor), orada bu hiç bildirilmemiş, ve proje canlı —
v1.0.30 2026-05-17, `linux_usbfs.c`'ye son dokunuş 2026-07-17, dışarıdan açılan
issue'lara medyan ilk cevap 3 saat.

**Çerçeve kaderi belirliyor.** "VM'imde passthrough bozuk" ölüyor: birebir
benzeri bir issue 33 dakikada kapsam dışı kapandı. Yaşayan çerçeve **API
sözleşmesi** — `libusb_wrap_sys_device()`'ın doxygen'i *"This is a non-blocking
function; no requests are sent over the bus"* diyor (birebir alıntı, `core.c`),
oysa Linux'ta wrap yolu telde `GET_CONFIGURATION` gönderiyor. Başlık mimari
asimetri; ölçülen bayt **destekleyici kanıt**, başlık değil.

**Sıra tamamlandı: bayt ölçüldü ve `0x00` çıktı** (2026-08-07) → "Ayıracak
ölçüm". Yani mekanizma kapandı ve rapor kendini yazdı. Yayımlanmadan önce
kaynağın dört iddiası master'a karşı birebir doğrulandı: `op_wrap_sys_device()`
gerçekten `NULL` geçiyor, `initialize_device()` `sysfs_dir` doluysa erken
dönüyor, `usbfs_get_active_config()` isteği aynen bu parametrelerle gönderiyor,
ve `-1` yalnızca `op_set_configuration()` ile onarılabiliyor (yedi atamanın
tek onaranı o) — yani durum **yapışkan**. Mükerrer de arandı: `wrap_sys_device`
geçen tek issue #1400, 2023'te kapanmış ve başka konu.

QEMU'ya rapor **ayrı ve isteğe bağlı** kalıyor, yalnızca mekanizmadan bağımsız
kusurla: `usb_host_detach_kernel()`'in hiç başarısızlık yolu yok — descriptor
çağrısı düşünce sessizce dönüyor ve detach etmediği arayüzleri `detached = true`
diye kaydediyor.

libusb'nin `AGENTS.md`'si `Assisted-by:` trailer'ını **commit ve PR açıklaması**
için şart koşuyor; issue için bir şart yazmıyor, ama aynı ruhla rapora da
konuyor (`claude-code:claude-opus-5` — en özgül model adı isteniyor).

## vfioctl bunu neden yazmıyor

Araç yetenek satırını domain tanımına yazmıyor, üç sebeple:

1. **Her USB devri için gerekli değil** — Razer alıcısı varsayılan kipte
   sorunsuz devredildi. Aracın koşulsuz yazması "her ihtimale karşı" olurdu.
2. **`guest usb`'nin sözü "iz bırakmam"dı.** Satır başlangıç anında etkili
   olduğu için oraya gömülemez; yazmak misafiri yeniden başlatmayı gerektirir.
3. **Sebep bilinmiyor.** Açıklanmamış bir düzeltmeyi aracın varsayılanı yapmak,
   onu bir daha kimsenin sorgulamayacağı bir yere koymak olur.

Kurulumunda gerekiyorsa satır elle bir kez yazılır. Yazılıp yazılmadığını
`virsh dumpxml <domain>` söyler.
