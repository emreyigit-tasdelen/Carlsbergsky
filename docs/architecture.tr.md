# Mimari

[← README](../README.tr.md) · [English](architecture.md) · **Türkçe**

Carlsbergsky tek dosyalık bir Python aracıdır (`install_ksc.py`). Kalıcı veri modeli, veritabanı veya sunucu bileşeni yoktur; bu nedenle ER diyagramı içermez. Durum yalnızca hedef sunucudaki `~/kscdistr` klasöründe ve resmi aracın `/var/opt/kaspersky/ksc_fast_install/` klasöründe tutulur.

## Kod bileşenleri

| Bileşen (`install_ksc.py`) | Sorumluluk |
|---|---|
| `find_files`, `read_fast_bundle` | Sürüm klasörünü tarar; `ksc64*`, `ksc-web-console*` paketlerini (deb/rpm) ve `ksc_fast_install` arşivini bulur, arşivi bellekte açar |
| `literal_table`, `ksc_version` | Resmi aracın kaynak kodundan sabit tabloları (`COMPAT_KSC_OS`, `NETSIZE_CPU/RAM/DISK_REQUIRED`) `ast.literal_eval` ile okur; paket adından sürümü çıkarır |
| `Remote` | paramiko ile SSH + SFTP: `run` (canlı çıktı, sudo parolası maskeleme), `out`, `must`, `upload`, `write_bytes`, `os_release`, `installed` |
| `check_resources` | Sunucunun `nproc`, `MemTotal`, `/var` boş alanını resmi eşiklerle karşılaştırır |
| `ensure_fqdn_loopback` | FQDN'in sunucuda loopback'e çözüldüğünü doğrular; gerekirse `/etc/hosts`'a ekler |
| `run_installer` | `~/kscdistr` içinde `./ksc_fast_install.py` çalıştırır |
| `connect` | Bağlantı ve kimlik doğrulama hatalarını anlaşılır mesajla bildirir |
| `main`, `finish` | Akış, onaylar, temizlik, sonuç özeti |

## Zaman çizelgesi

```mermaid
sequenceDiagram
    autonumber
    actor Y as Yönetici
    participant S as install_ksc.py
    participant R as Remote (paramiko)
    participant H as Hedef sunucu
    participant F as ksc_fast_install.py

    Y->>S: sürüm klasörü, IP, SSH bilgileri
    S->>S: find_files + read_fast_bundle
    S->>R: bağlan
    R->>H: cat /etc/os-release
    H-->>S: dağıtım ve sürüm (deb mi rpm mi)
    S->>R: installed("ksc64")
    R->>H: dpkg-query / rpm -q
    S->>S: COMPAT_KSC_OS ile OS kontrolü
    Y->>S: EULA onayı, FQDN, boyut, port
    S->>R: check_resources
    R->>H: nproc, MemTotal, df /var
    S->>R: ensure_fqdn_loopback
    R->>H: getent hosts FQDN
    S->>R: apt-get update / dnf makecache
    S->>R: dosyaları yükle (SFTP) + params.txt yaz
    R->>H: ~/kscdistr
    S->>R: run_installer
    R->>H: sudo ./ksc_fast_install.py
    H->>F: çalıştır
    F-->>Y: canlı çıktı (adım adım)
    F-->>S: çıkış kodu + kscadmin bilgisi
    S-->>Y: Web Console adresi ve giriş
```

## Çalışma klasörünün yaşam döngüsü

`~/kscdistr` (sunucuda) üç sonla biter:

```mermaid
stateDiagram-v2
    [*] --> Yok
    Yok --> Hazır: dosyalar yüklendi
    Hazır --> Çalışıyor: run_installer
    Çalışıyor --> Kalıcı: başarı
    Çalışıyor --> Silindi: hata (resmi araç geri aldı)
    Çalışıyor --> YarımKaldı: Ctrl+C / bağlantı koptu
    YarımKaldı --> Çalışıyor: script tekrar çalıştı, devam onaylandı
    YarımKaldı --> Silindi: sunucuda ksc_fast_install.py rollback ve elle silme
    Kalıcı --> [*]
    Silindi --> [*]
```

Koddaki karşılığı: `cleanup_on_error` yalnızca yükleme başladıktan sonra `True` olur; başarıda `False`'a çekilir, `KeyboardInterrupt`'ta `False` kalır. `finally` bloğu `cleanup_on_error` ise klasörü siler.

## Tasarım kararları

| Karar | Neden |
|---|---|
| Resmi `ksc_fast_install.py` sürülür, kurulum yeniden yazılmaz | DBMS, hesap ve servis kurulumu sürümle değişir; Kaspersky'nin aracı bunu zaten yönetir ve hatada kendi değişikliklerini geri alır |
| Uyumluluk ve kaynak eşikleri resmi aracın kaynağından okunur | Yeni sürüm klasörü verildiğinde script'te kod değişmez; kendi listemiz eskiyip yanlış sonuç vermez |
| Güncelleme modu yok | Resmi araç yalnız KSC kurulu olmayan sunucuda çalışır (kılavuz). Kapsam bilerek dar tutuldu |
| Paketler repoya girmez | Kaspersky lisanslı dağıtımı; boyutları yüzlerce MB. `.gitignore` ile dışarıda |
| Kaynak kontrolünü atlama yalnız sunucuya yüklenen kopyada yamalanır | Yerel dosya değişmez; yama, çağrı satırı bulunamazsa uyarıyla durur (araç yapısı değişmişse sessizce yanlış yama yapmaz) |
| `sudo -S` + PTY, parola çıktıdan maskelenir | PTY gönderilen parolayı geri yansıtır; ilk satır yutulur ve parola `******` ile değiştirilir |
| `params.txt` `chmod 600` | EULA ve adres bilgisi başkalarınca okunmasın |

## Bilinen sınırlar

Bkz. README → [Güvenlik ve sınırlar](../README.tr.md#güvenlik-ve-sınırlar).

---

<sub>© 2026 Emre Yiğit Taşdelen · MIT Lisansı</sub>
