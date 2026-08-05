# USB Bluetooth radyosu misafirde Kod 10 veriyor

🇬🇧 **Summary.** A USB Bluetooth radio handed to a Windows guest enumerates
under its correct name but fails to start (Code 10 / `CM_PROB_FAILED_START`,
`0xC0000001`). Removing the `usb-host.hostdevice` QEMU capability from the
domain makes it work: libvirt then addresses the device with
`hostbus=`/`hostaddr=` instead of an already-open file descriptor. **The
workaround is empirical and reproducible; the cause is not known.** Five
explanations were measured and ruled out — they are listed below so nobody
spends a session re-deriving them.

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
`usb_host_detach_kernel()` çağırıyor (16 arayüz için `USBDEVFS_DISCONNECT`),
ama sonuç aynı olmuyor:

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

## Sebep bilinmiyor — ve elenenler bunun parçası

Geriye yalnızca olgu kalıyor: `hostbus`/`hostaddr` çalışıyor, `hostdevice=`
çalışmıyor, aradaki farkın bu cihazda neyi değiştirdiği bilinmiyor. Aşağıdaki
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

Dışarıda belgeli değil. Yetenek kaldırmayı öneren tek rapor (unRAID, Intel
3168, Windows misafiri, aynı `0xC0000001`) düzeltmenin **işe yaradığını**
biliyor, **neden** işe yaradığını bilmiyor.

## Bilinen risk

`<qemu:capabilities>` libvirt'te bir *test* olanağı olarak belgeli. Kalıcı bir
kurulumu ona dayandırmak, belgelenmemiş bir yüzeye bağlanmak demek — bugün
çalışıyor, bir libvirt sürümünde sessizce değişebilir. Ölçülen asimetri QEMU
tarafında bir kusur gibi durduğu için doğru uzun vadeli yol muhtemelen yukarı
akışa bildirmek.

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
