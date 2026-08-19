# vfioctl

Bir dizüstünün dGPU'sunu Windows misafirine devreden VFIO passthrough
kurulumunu **kuran, ölçen ve süren** CLI aracı. Kapsam ve yol haritası →
[README.md](README.md).

Araştırma notları bu depoda **değil**: `~/Belgeler/pars/vfioctl/NOTLAR.md`.
Donanım olguları, tuzaklar ve kararlar (K1–K21) orada; bir davranışın *neden*
öyle olduğu sorusu önce oraya sorulur.

## Dil

Konuşma ve yorum-dışı belgeler (README, bu dosya) Türkçe. **Kod, dosya/klasör
adları, tanımlayıcılar, docstring'ler, yorumlar ve commit mesajları İngilizce.**
Kullanıcıya basılan çıktı Türkçe.

## Kapı — bu projenin taşıyıcı ilkesi

**Yazan hiçbir alt komut profil eşleşmesi olmadan koşmaz.** Tek soru sahibi
`core.doctor.gate()`; yazan her komut ona sorar ve `False` gelirse durur.
Kendi denetimini türeten komut yazılmaz — kapının tek olmasının sebebi, yeni
bir alt komudun onu kazara atlayamaması.

- **`--force` yok ve eklenmez.** Bayrak, "bunu henüz desteklemiyoruz"u "uyarmıştık"a
  çevirir; kapının var olma sebebi tam olarak o sonucu önlemek. Yarı çalışan bir
  passthrough kurulumu, hiç kurulmamış olmaktan kötüdür — kullanıcı çalışan bir
  masaüstünü kaybeder.
- **`doctor` her makinede koşar ve hiçbir şey yazmaz.** Profil yoksa da çalışır:
  donanımı keşfeder ve neyin uymadığını tek tek söyler. Taşımak isteyenin
  ihtiyacı budur; tümden red, teşhis vermediği için taşımayı imkânsız kılar.
- **İki severite.** *Sert* ölçüt karşılanmazsa tasarım **çalışamaz** (IOMMU grubu
  paylaşımlı, host ekranını taşıyacak iGPU yok) → kapı kapanır. *Yumuşak* ölçüt
  bir farktır, kusur değil (çekirdek lezzeti, kartın tam varyantı) → uyarır,
  geçer. Bir çekirdek yükseltmesi aracı durduramaz.
- **Vaat verilmez.** Araç kendine yeter ama **evrensel değildir**; başka
  donanımda çalışacağı söylenmez. Yeni makine desteklemek = `profiles/` altına
  bir `.toml` + o makinede koşmuş bir tur.

## Dokunulmaz olanlar

- **`win11` domain'i ve `~/.images/win11.qcow2`** — çalışan misafir. Yıkıcı yol
  ona **ad ve disk yoluyla** kapalı (`guest/build.py`), koruma kaldırılmaz.
  Denemeler `win11-test` üzerinde yapılır.
- **Kartın bind yollarında tek yazar vardır ve o hook'tur.** Hiçbir alt komut
  kartı bağlamaz, çözmez, probe etmez; `install` dosya yazar, `selftest` misafir
  başlatıp okur. İkinci bir yazar, bu makineyi üç kez kilitleyen yolun ta
  kendisi. XML tarafındaki karşılığı kartın `managed='no'`'sudur (K8) ve
  kaldırılmaz — `'yes'` libvirt'i aynı yollara ikinci yazar yapardı.
  - **Yasağın konusu kartın yollarıdır, "PCI" sözcüğü değil** (2026-08-19'da
    ölçülerek ayrıldı). Kart **dışındaki** bir PCI cihazı için libvirt tek yazar
    olabilir: `guest nvme` ve `build --system-nvme` NVMe denetleyicisini
    `managed='yes'` ile domain'in kalıcı tanımına yazar, bağlamayı domain
    başlarken libvirt yapar, araç hiçbir sysfs yoluna dokunmaz. İzin veren üç
    ölçüm, üçü de bu maddenin şartı: (1) `nvme` sürücüsünün boşaltma dansı yok —
    unbind'da bırakıyor, probe'da geri alıyor (2026-08-18, boş Crucial
    `0000:02:00.0`; **tek disk, tek makine, tek tarih** — sınıfa genellenmedi);
    (2) hook o cihaza **yazamaz**, çünkü her eylemi `for dev in $DEVICES`
    döngüsüdür ve `$DEVICES` `vfio.conf`'tan gelir, domain'in XML'inden değil;
    (3) NVMe `vfio.conf`'a araç eliyle **giremez** — `install` o satırı dGPU'nun
    IOMMU grubundan yazar ve grupta fazladan üye varsa kurulumu tümden reddeder
    (`core/install.py:97-100`). **Üçünden biri düşerse istisna da düşer.**
  - **USB hostdev de dışarıda, ama başka sebeple** (2026-08-05, `guest usb`
    yazılmadan önce tartışıldı). Üç ölçüm: hook `type='pci'` süzüyor — yalnız
    USB taşıyan bir XML'e `no handover` diyor; canlı takmada hook zaten hiç
    koşmuyor; `managed` özniteliğini libvirt **yalnızca PCI** için okuyup USB'yi
    kendisi ayırıyor. **Bu üç ölçüm NVMe'ye taşınmaz, ve taşınmaya çalışılırsa
    yanlış güven verir:** NVMe hostdev'i `type='pci'` olduğu için hook onu
    `hostdev:` satırında **görür** — koruyan şey süzgeç değil `$DEVICES`'ın
    kapsamıdır (yukarıdaki 2. ve 3. ölçüm).
  - **Devrin hükmünü bu madde vermez:** hangi cihazın kalkabileceğini
    `core.inventory` söyler (K14'ün sert koruması, bayraksız), ve hüküm **yazma
    anında** koşar — bağlama `virsh start`'ta olur. Arada adres kayabilir
    (2026-08-17'de kaydı), köprü `<metadata>`'daki kimlik kaydıdır.
  - **Dördüncü bir cihaz sınıfı gelirse önce bu madde güncellenir, sonra kod
    yazılır.** `guest nvme` tersini yaptı: kod 2026-08-18'de girdi, çelişkiyi
    ertesi günkü kod incelemesi buldu (madde 12) ve bu madde bir gün boyunca
    davranışı yanlış anlattı. Sorulacak sorular yukarıdaki üç ölçümdür.
- **Paket kurulmaz.** Eksik paket ölçülür ve komutu basılır. Kurmak, bir AUR
  yardımcısının disiplinini (özellikle: asla `--noconfirm`, PKGBUILD diff'i
  okunur) burada yeniden üretmek demek olurdu.
- **`/etc`'e yazan tek yol `core/sysfile.py`.** Araç root olarak koşmaz; yetki
  yazma anında, tek tek alınır. `doctor` hiçbir zaman yazmaz.
- **Kartı tutan yollara yazma sırası.** `unbind` / `drivers_probe` yollarına
  `lsmod | grep ^nvidia` **boş olmadıkça** yazılmaz; yazılırsa çekirdek
  `R` durumunda süresiz döner ve tek çıkış reboot. Bu üç kez oldu.
  `driver_override` bu listede değil — saf öznitelik yazımı, ve unload'dan
  **önce** yazılmalı.

## Tur disiplini — iddia değil ölçüm

Bu projede "çalışıyor" demenin bedeli ölçmektir; kod da böyle yazılır.

- **Başarılı bir yazma iddiadır, okunan durum kanıttır.** `reg add`'in 0
  dönmesi autologon'un kurulduğunu göstermez; `guest-get-users`'ın kullanıcıyı
  sayması gösterir. Yeni bir adım eklerken sorulacak soru: *bunun olduğunu
  neyden okuyacağım?*
- **Sabit bir `sleep` sonrası yoklamak sınama değildir.** Yeniden başlatmanın
  ölçütü, misafirin **kendi açılış zamanının değişmesi**dir; hiç başlamamış bir
  kapanma, hızlı bitmiş bir kapanmayla birebir aynı görünür.
- **Bir tur geçti diye "çalışıyor" denmez.** Olasılıksal arızalarda doğru soru
  "tekrarlanıyor mu" değil **"görev döngüsü ne"**; devir iddiası art arda en az
  **beş tur** ile sınanır.
- **Sessiz başarısızlık varsayılan sanılmaz.** Kurulmamış bir hook, kanalı
  olmayan bir ajan, bulunamamış bir cevap dosyası — üçü de dışarıdan "hiçbir şey
  olmadı" ile birebir aynı görünür. Her yeni adım, başarısızlığının nasıl
  görüneceği yazılarak eklenir.

## Kod

- **Her dosyanın başlığı gerekçeyi taşır.** Neyin neden öyle yazıldığı — hangi
  API'nin sessizce başarısız olduğu, hangi sıranın zorunlu olduğu — koddan
  değerli. Dosya değiştirilmeden önce başlığı okunur; davranış değişiyorsa
  başlık da güncellenir.
- **Bağımlılık yok.** Standart kütüphane (`tomllib` dahil) + sistemde zaten olan
  araçlar (`virsh`, `qemu-img`, `xorriso`). Yeni bağımlılık bir karardır.
- **Kişisel hiçbir değer depoya girmez:** hesap adı, parola, yerel ayar, ISO
  yolu çalışma anında verilir. Depoya **şablon** girer. `.githooks/pre-commit`
  gitleaks koşar; yeni klonda `git config core.hooksPath .githooks`.
- **Bir işin tek çalıştırılabilir girişi olur.** Düzeltme yeni dosya açmaz,
  mevcudunun yerine geçer.
