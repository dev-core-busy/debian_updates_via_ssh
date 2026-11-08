"use strict";
/* apt-via-ssh Oberflaeche -- ohne Fremdbibliotheken.
   Grundsatz: Daten vom Ziel (Paketnamen, Beschreibungen, Protokolle) werden
   ausschliesslich ueber textContent eingefuegt, nie als HTML. */

const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (s) => document.querySelector(s);
const Z = { z: null, ziele: [], updates: null, auswahl: new Set(), installiert: null,
            dist: null, distSim: null, info: null, laeuft: false };

// ------------------------------------------------------------------ Helfer
function el(tag, attrs, ...kinder) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kinder.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : String(c));
  }
  return e;
}
// replaceChildren() macht aus null den Text "null" -> leere Eintraege vorher entfernen
function setze(ziel, ...kinder) {
  ziel.replaceChildren(...kinder.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}
const abz = (text, art) => el("span", { class: "abzeichen " + (art || "") }, text);
const schlafen = (ms) => new Promise((r) => setTimeout(r, ms));
function groesse(b) {
  if (!b) return "0 B";
  const e = ["B", "KiB", "MiB", "GiB"]; let i = 0;
  while (b >= 1024 && i < e.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(1) : b) + " " + e[i];
}
function meldung(art, ...inhalt) { return el("div", { class: "meldung " + art }, ...inhalt); }

async function api(pfad, daten) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (daten !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(daten);
  }
  let r;
  try { r = await fetch(pfad, opt); }
  catch (e) { throw new Error("Oberfläche nicht erreichbar – läuft apt-via-ssh-gui noch?"); }
  let j = {};
  try { j = await r.json(); } catch (e) { /* leer */ }
  if (!r.ok) throw new Error(j.fehler || ("HTTP " + r.status));
  return j;
}

function dialog(titel, inhalt, knoepfe) {
  const dlg = $("#dlg");
  $("#dlg-titel").textContent = titel;
  setze($("#dlg-inhalt"), ...[].concat(inhalt));
  const leiste = $("#dlg-knoepfe");
  setze(leiste, );
  return new Promise((res) => {
    let erledigt = false;
    const fertig = (w) => { if (erledigt) return; erledigt = true; dlg.close(); res(w); };
    dialog.knoepfe = {};
    for (const k of knoepfe) {
      const b = el("button", { type: "button", class: k.class || "", onclick: () => fertig(k.wert) }, k.text);
      if (k.aus) b.disabled = true;
      dialog.knoepfe[k.wert ?? "abbrechen"] = b;
      leiste.append(b);
    }
    dlg.onclose = () => fertig(null);
    dlg.showModal();
  });
}
async function bestaetigen(titel, text, knopf = "OK", art = "haupt") {
  const w = await dialog(titel, [el("p", {}, text)],
    [{ text: "Abbrechen", wert: null, class: "leise" }, { text: knopf, wert: "ok", class: art }]);
  return w === "ok";
}
function fehlerZeigen(e) {
  dialog("Fehler", [meldung("fehler", el("p", {}, e.message || String(e)))], [{ text: "Schließen", wert: null }]);
}

// Schaltflaechen, die waehrend eines Auftrags gesperrt sind
const AKTIONEN = ["#btn-einrichten", "#btn-updates-pruefen", "#btn-einspielen", "#btn-autoremove",
  "#btn-installieren", "#btn-dist-sim", "#btn-dist-start", "#btn-dist-zurueck", "#btn-neustart",
  "#btn-entfernen", "#btn-trennen", "#btn-zu-wartung"];
function beschaeftigt(an) {
  Z.laeuft = an;
  for (const s of AKTIONEN) { const b = $(s); if (b) b.dataset.gesperrt = an ? "1" : ""; }
  aktualisiereSperren();
}
function aktualisiereSperren() {
  for (const s of AKTIONEN) {
    const b = $(s); if (!b) continue;
    const eigen = b.dataset.eigenAus === "1";
    b.disabled = Z.laeuft || eigen;
  }
}
function eigenAus(sel, aus) { const b = $(sel); b.dataset.eigenAus = aus ? "1" : ""; aktualisiereSperren(); }

// --------------------------------------------------------------- Protokoll
async function folgen(a, fertig) {
  const karte = $("#karte-protokoll"), pre = $("#protokoll"), st = $("#protokoll-status");
  karte.hidden = false;
  $("#protokoll-titel").textContent = a.titel;
  setze(pre, );
  st.className = "abzeichen info"; st.textContent = "läuft";
  beschaeftigt(true);
  let ab = 0, r = a;
  try {
    while (true) {
      r = await api(`/api/auftrag?id=${encodeURIComponent(a.id)}&ab=${ab}`);
      if (r.zeilen.length) {
        pre.append(document.createTextNode(r.zeilen.join("\n") + "\n"));
        ab = r.anzahl;
        if ($("#f-scroll").checked) pre.scrollTop = pre.scrollHeight;
      }
      st.textContent = (r.status === "laeuft" ? "läuft" : r.status === "ok" ? "fertig" : "Fehler") + ` · ${r.dauer} s`;
      if (r.status !== "laeuft") break;
      await schlafen(800);
    }
  } catch (e) {
    st.className = "abzeichen fehler"; st.textContent = "Verbindung zur Oberfläche verloren";
    beschaeftigt(false);
    throw e;
  }
  st.className = "abzeichen " + (r.status === "ok" ? "ok" : "fehler");
  beschaeftigt(false);
  if (fertig) await fertig(r);
  return r;
}
async function auftrag(pfad, daten, fertig) {
  try {
    const a = await api(pfad, daten || {});
    return await folgen(a, fertig);
  } catch (e) { fehlerZeigen(e); return null; }
}

// ------------------------------------------------------------------ Zustand
async function zustandLaden() {
  Z.z = await api("/api/zustand");
  zeichneZustand();
}
function zeichneZustand() {
  const z = Z.z, st = $("#status");
  if (z.verbunden) {
    st.className = "status " + (z.rolle === "keine" ? "halb" : "an");
    st.textContent = `${z.beschreibung} · ${z.rolle_text}`;
  } else {
    st.className = "status aus"; st.textContent = "nicht verbunden";
  }
  $("#btn-trennen").hidden = !z.verbunden;
  $("#bereich-verbunden").hidden = !z.verbunden;
  $("#karte-ziel").hidden = z.verbunden;
  if (z.verbunden) {
    eigenAus("#btn-entfernen", !z.ist_admin || !(z.profil && z.profil.wart_benutzer));
    eigenAus("#btn-einrichten", !z.ist_admin);
    zeichneHosts();
  }
}

// ------------------------------------------------------------- Ziel/Profil
async function profileLaden() {
  const r = await api("/api/ziele");
  Z.ziele = r.ziele || [];
  const sel = $("#f-profil");
  setze(sel, el("option", { value: "" }, "– neues Ziel –"));
  Z.ziele.forEach((p, i) => {
    const port = p.port && p.port != 22 ? ":" + p.port : "";
    const wart = p.wart_benutzer ? ` · Wartung: ${p.wart_benutzer}` : "";
    sel.append(el("option", { value: String(i) }, `${p.host}${port}${wart}${p.zuletzt ? " · " + p.zuletzt : ""}`));
  });
  wartungKnopf();
}
function profilZuForm(p) {
  $("#f-host").value = p.host || "";
  $("#f-port").value = p.port || 22;
  $("#f-benutzer").value = p.benutzer || "root";
  $("#f-schluessel").value = p.schluessel || "";
  $("#f-zertifikat").value = p.zertifikat || "";
  for (const f of ["#f-passwort", "#f-passphrase", "#f-rootpw"]) $(f).value = "";
  if (p.wart_benutzer) $("#f-wartname").value = p.wart_benutzer;
  wartungKnopf();
}
function aktuellesProfil() {
  const h = $("#f-host").value.trim(), p = Number($("#f-port").value || 22);
  return Z.ziele.find((z) => z.host === h && Number(z.port || 22) === p);
}
function wartungKnopf() {
  const p = aktuellesProfil();
  $("#btn-wartung").disabled = !(p && p.wart_schluessel);
  $("#btn-wartung").title = p && p.wart_schluessel ? `als ${p.wart_benutzer} mit gespeichertem Schlüssel` :
    "Für dieses Ziel ist noch kein Wartungszugang eingerichtet";
}
async function schluesselLaden() {
  try {
    const r = await api("/api/schluessel");
    setze($("#liste-schluessel"), ...r.schluessel.map((s) => el("option", { value: s })));
    setze($("#liste-zertifikate"), ...r.zertifikate.map((s) => el("option", { value: s })));
  } catch (e) { /* unwichtig */ }
}
function formWerte() {
  return {
    host: $("#f-host").value.trim(), port: Number($("#f-port").value || 22),
    benutzer: $("#f-benutzer").value.trim(), passwort: $("#f-passwort").value,
    schluessel: $("#f-schluessel").value.trim(), passphrase: $("#f-passphrase").value,
    zertifikat: $("#f-zertifikat").value.trim(), root_passwort: $("#f-rootpw").value,
  };
}
function zielHinweis(text, art) {
  const h = $("#ziel-hinweis");
  h.textContent = text || "";
  h.style.color = art === "fehler" ? "var(--fehler)" : "";
}

async function hostkeyDialog(d, hk) {
  const geaendert = hk.status === "geaendert";
  const inhalt = [
    geaendert
      ? meldung("fehler", el("p", {}, el("strong", {}, "Der Hostschlüssel hat sich GEÄNDERT.")),
          el("p", {}, "Entweder wurde das System neu installiert – oder jemand gibt sich als dieses System aus. " +
                      "Nur annehmen, wenn Sie den neuen Fingerabdruck auf einem sicheren Weg geprüft haben."))
      : el("p", {}, `Erste Verbindung zu ${d.host}:${d.port}. Bitte vergleichen Sie den Fingerabdruck mit dem des Zielsystems ` +
                    "(dort: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)."),
    ...hk.schluessel.map((k) => el("div", { class: "fingerabdruck" }, `${k.typ}  ${k.fingerabdruck}`)),
  ];
  const w = await dialog(geaendert ? "Hostschlüssel geändert!" : "Unbekannter Hostschlüssel", inhalt, [
    { text: "Abbrechen", wert: null, class: "leise" },
    { text: geaendert ? "Ersetzen und verbinden" : "Annehmen und verbinden", wert: "ok", class: geaendert ? "gefahr" : "haupt" },
  ]);
  return w === "ok";
}

async function verbinden(modus) {
  const d = formWerte(); d.modus = modus;
  if (!d.host) { zielHinweis("Bitte eine Zieladresse angeben", "fehler"); return; }
  for (const b of ["#btn-verbinden", "#btn-wartung"]) $(b).disabled = true;
  zielHinweis("Verbinde …");
  try {
    let r = await api("/api/verbinden", d);
    if (r.hostkey) {
      zielHinweis("");
      if (!(await hostkeyDialog(d, r.hostkey))) { zielHinweis("Abgebrochen"); return; }
      await api("/api/hostkey-annehmen", { host: d.host, port: d.port, ersetzen: r.hostkey.status === "geaendert" });
      zielHinweis("Verbinde …");
      r = await api("/api/verbinden", d);
      if (r.hostkey) throw new Error("Hostschlüssel wurde nicht gespeichert");
    }
    for (const f of ["#f-passwort", "#f-passphrase", "#f-rootpw"]) $(f).value = "";
    zielHinweis("");
    Z.z = r; Z.updates = null; Z.installiert = null; Z.distSim = null; Z.auswahl.clear();
    zeichneZustand();
    await profileLaden();
    zeigeReiter("voraussetzungen");
    infoLaden();
    voraussetzungenPruefen();
  } catch (e) {
    zielHinweis(e.message, "fehler");
  } finally {
    $("#btn-verbinden").disabled = false; wartungKnopf();
  }
}

// ---------------------------------------------------------------- Systeminfo
async function infoLaden() {
  const box = $("#systeminfo");
  try {
    const i = await api("/api/info");
    Z.info = i;
    const k = (t, w, extra) => el("div", { class: "kachel" }, el("b", {}, t), w, extra || null);
    const frei = Number(i.frei_var_mb || 0);
    setze(box, 
      k("System", i.os), k("Rechner", i.rechnername), k("Kernel", i.kernel), k("Architektur", i.arch),
      k("Pakete", i.pakete),
      k("Frei in /var", groesse(frei * 1048576), frei < 1024 ? abz("knapp", "warn") : null),
      k("Neustart", i.neustart === "ja" ? abz("erforderlich", "warn") : abz("nicht nötig", "ok")),
      i.auftrag === "laeuft"
        ? k("Auftrag auf dem Ziel", abz("läuft", "info"),
            el("button", { class: "leise", onclick: () => auftrag("/api/verfolgen", {}, () => infoLaden()) }, "verfolgen"))
        : null,
      i.dpkg_ok === "nein" ? k("dpkg", abz("unvollständig", "fehler")) : null,
    );
  } catch (e) {
    Z.info = null;
    setze(box, el("div", { class: "kachel" }, el("b", {}, "System"), abz(e.message, "warn")));
  }
}

// ------------------------------------------------------------ Voraussetzungen
const STATUS_ART = { OK: "ok", NEU: "ok", WARNUNG: "warn", FEHLER: "fehler", INFO: "info" };
async function voraussetzungenPruefen() {
  const tb = $("#tab-vor tbody");
  setze(tb, el("tr", {}, el("td", { colspan: 3, class: "hinweis" }, "Prüfe … (Anmeldung, Rechte, Tunnel, Einrichtung)")));
  $("#vor-hinweis").textContent = "";
  try {
    const r = await api("/api/voraussetzungen");
    setze(tb, ...r.schritte.map((s) => el("tr", {},
      el("td", {}, abz(s.status === "NEU" ? "OK" : s.status, STATUS_ART[s.status] || "")),
      el("td", {}, el("strong", {}, s.schritt)), el("td", {}, s.text))));
    if (Z.z.rolle === "keine") {
      const pw = el("input", { type: "password", placeholder: "root-Passwort", autocomplete: "new-password" });
      tb.append(el("tr", {}, el("td", {}, abz("?", "warn")), el("td", {}, el("strong", {}, "root via su")),
        el("td", {}, el("div", { class: "knopfleiste" }, pw,
          el("button", { onclick: async () => {
            try { Z.z = await api("/api/root-passwort", { passwort: pw.value }); pw.value = ""; zeichneZustand(); voraussetzungenPruefen(); }
            catch (e) { fehlerZeigen(e); } } }, "Übernehmen")))));
    }
    $("#einrichten-box").hidden = !r.einrichtbar;
    $("#vor-hinweis").textContent = r.bereit ? "Alles bereit – Updates sind möglich."
      : (r.einrichtbar ? "Es fehlt noch etwas – „Einrichten“ behebt das." : "Es fehlt noch etwas.");
    const p = Z.z.profil || {};
    $("#btn-zu-wartung").hidden = !(r.einrichtbar && p.wart_schluessel);
  } catch (e) {
    setze(tb, el("tr", {}, el("td", { colspan: 3 }, meldung("fehler", e.message))));
  }
}
async function einrichten() {
  const name = $("#f-wartname").value.trim();
  const ok = await dialog("Wartungszugang einrichten", [
    el("p", {}, `Auf ${Z.z.beschreibung} wird Folgendes eingerichtet (bereits Vorhandenes bleibt unverändert):`),
    el("ul", {},
      el("li", {}, `Benutzer „${name}“ ohne Passwort, Anmeldung nur mit einem neuen SSH-Schlüssel dieses Rechners`),
      el("li", {}, "Schlüssel-Einschränkung (restrict): kein Terminal, kein Agent, kein X11; Tunnel nur auf localhost"),
      el("li", {}, "/usr/local/sbin/apt-via-ssh-helper (Hilfsskript für die Paketverwaltung)"),
      el("li", {}, "/etc/sudoers.d/apt-via-ssh: sudo nur für dieses Hilfsskript"),
      $("#f-weiterleitung").checked ? el("li", {}, "sshd-Regel nur für diesen Benutzer: ausschließlich Reverse-Tunnel, keine Weiterleitung ins Netz des Ziels") : null,
      el("li", {}, "falls sudo fehlt: Installation über den Tunnel")),
  ], [{ text: "Abbrechen", wert: null, class: "leise" }, { text: "Einrichten", wert: "ok", class: "haupt" }]);
  if (ok !== "ok") return;
  await auftrag("/api/einrichten", { wart_benutzer: name, weiterleitung: $("#f-weiterleitung").checked }, async (r) => {
    await zustandLaden(); await profileLaden();
    await voraussetzungenPruefen();
    if (r.status === "ok") $("#btn-zu-wartung").hidden = false;
  });
}

// ------------------------------------------------------------------ Updates
function zeichneUpdates() {
  const e = Z.updates, tb = $("#tab-updates tbody"), box = $("#upd-meldungen");
  setze(box, ); setze(tb, );
  if (!e) { $("#upd-zaehler").textContent = ""; eigenAus("#btn-einspielen", true); return; }
  if (e.gesperrt && e.gesperrt.length) box.append(blockiertMeldung(e.gesperrt));
  if (!e.aktualisiert.length) box.append(meldung("ok", el("p", {}, "Das System ist auf dem neuesten Stand.")));
  const zurueck = e.aktualisiert.filter((p) => p.zurueckgehalten).length;
  if (zurueck || e.neu.length || e.entfernt.length) {
    box.append(meldung("warn", el("p", {},
      `${zurueck} Update(s) brauchen ein „full-upgrade“` +
      (e.neu.length ? `, dafür werden ${e.neu.length} neue Paket(e) installiert` : "") +
      (e.entfernt.length ? ` und ${e.entfernt.length} Paket(e) entfernt` : "") +
      ". Vor dem Einspielen zeigt die Vorschau genau, was passiert.")));
  }
  const filter = $("#f-filter-updates").value.trim().toLowerCase();
  for (const p of e.aktualisiert) {
    if (filter && !p.name.toLowerCase().includes(filter)) continue;
    const cb = el("input", { type: "checkbox" });
    cb.checked = Z.auswahl.has(p.name);
    cb.addEventListener("change", () => { cb.checked ? Z.auswahl.add(p.name) : Z.auswahl.delete(p.name); updZaehler(); });
    tb.append(el("tr", {},
      el("td", {}, cb), el("td", {}, el("strong", {}, p.name)),
      el("td", { class: "mono" }, p.alt), el("td", { class: "mono" }, p.neu),
      el("td", { class: "hinweis" }, p.herkunft),
      el("td", {}, p.sicherheit ? abz("Sicherheit", "fehler") : null, p.zurueckgehalten ? abz("full-upgrade", "warn") : null)));
  }
  updZaehler();
}
function updZaehler() {
  const e = Z.updates; if (!e) return;
  const n = e.aktualisiert.length, s = e.aktualisiert.filter((p) => p.sicherheit).length;
  $("#upd-zaehler").textContent = `${Z.auswahl.size} von ${n} ausgewählt` + (s ? ` · ${s} Sicherheitsupdates` : "");
  eigenAus("#btn-einspielen", Z.auswahl.size === 0);
}
function blockiertMeldung(hosts) {
  return meldung("warn", el("p", {}, "Diese Repository-Hosts hat der Proxy blockiert, ihre Pakete fehlen deshalb:"),
    el("div", { class: "knopfleiste" }, ...hosts.map((h) => el("button", { onclick: async (ev) => {
      try { await api("/api/erlauben", { host: h }); ev.target.disabled = true; ev.target.textContent = h + " ✓ freigegeben"; await zustandLaden(); }
      catch (e) { fehlerZeigen(e); } } }, `${h} freigeben`))),
    el("p", { class: "hinweis" }, "Danach bitte erneut prüfen."));
}
async function updatesPruefen() {
  await auftrag("/api/pruefen", {}, async (r) => {
    if (r.status === "ok") {
      Z.updates = r.ergebnis;
      Z.auswahl = new Set(r.ergebnis.aktualisiert.map((p) => p.name));
      zeichneUpdates();
    }
    infoLaden();
  });
}
async function simulierenUndBestaetigen(modus, pakete, titel) {
  let s;
  try { s = await api("/api/simulieren", { modus, pakete }); }
  catch (e) { fehlerZeigen(e); return; }
  const nichts = !s.aktualisiert.length && !s.neu.length && !s.entfernt.length;
  const liste = (t, xs, f, offen) => xs.length ? el("details", { open: offen || null },
    el("summary", {}, `${t} (${xs.length})`), el("ul", {}, ...xs.map((x) => el("li", {}, f(x))))) : null;
  const haken = el("input", { type: "checkbox" });
  const inhalt = [
    el("div", { class: "zahlen" },
      el("div", {}, el("strong", {}, s.aktualisiert.length), "aktualisiert"),
      el("div", {}, el("strong", {}, s.neu.length), "neu installiert"),
      el("div", { class: s.entfernt.length ? "rot" : "" }, el("strong", {}, s.entfernt.length), "entfernt"),
      el("div", {}, el("strong", {}, groesse(s.bytes)), "Download")),
    s.zusammenfassung ? el("p", { class: "hinweis" }, "apt: " + s.zusammenfassung) : null,
    s.fehler.length ? meldung("fehler", ...s.fehler.map((f) => el("p", {}, f))) : null,
    nichts && !s.fehler.length ? meldung("ok", el("p", {}, "Nichts zu tun.")) : null,
    liste("Wird ENTFERNT", s.entfernt, (x) => `${x.name} ${x.alt}`, true),
    liste("Wird aktualisiert", s.aktualisiert, (x) => `${x.name}: ${x.alt} → ${x.neu}`),
    liste("Wird neu installiert", s.neu, (x) => `${x.name} ${x.neu}`),
    s.entfernt.length ? el("label", { class: "haken" }, haken, "Ich habe die zu entfernenden Pakete geprüft") : null,
    el("p", { class: "hinweis" }, "Pakete werden zuerst über den Tunnel geladen; die Installation läuft danach auf dem Ziel " +
      "unabhängig von der SSH-Verbindung. Geänderte Konfigurationsdateien bleiben erhalten."),
  ];
  const blockiert = !s.ok || nichts || s.fehler.length > 0;
  const p = dialog(titel, inhalt, [{ text: "Abbrechen", wert: null, class: "leise" },
    { text: "OK – jetzt ausführen", wert: "ok", class: s.entfernt.length ? "gefahr voll" : "haupt", aus: blockiert || s.entfernt.length > 0 }]);
  if (s.entfernt.length && !blockiert) haken.addEventListener("change", () => { dialog.knoepfe.ok.disabled = !haken.checked; });
  if ((await p) !== "ok") return;
  await auftrag("/api/ausfuehren", { modus, pakete }, async (r) => {
    infoLaden();
    if (r.status === "ok" && modus !== "install") await updatesPruefen();
  });
}
function einspielen() {
  const e = Z.updates; if (!e) return;
  const alle = e.aktualisiert.map((p) => p.name);
  const sel = alle.filter((n) => Z.auswahl.has(n));
  if (sel.length === alle.length) {
    const voll = e.aktualisiert.some((p) => p.zurueckgehalten);
    simulierenUndBestaetigen(voll ? "full-upgrade" : "upgrade", [], "Alle Updates einspielen");
  } else {
    simulierenUndBestaetigen("only-upgrade", sel, `${sel.length} ausgewählte Update(s) einspielen`);
  }
}

// ---------------------------------------------------------------- Installiert
async function installiertLaden() {
  $("#inst-zaehler").textContent = "lade …";
  try { Z.installiert = (await api("/api/installiert")).pakete; zeichneInstalliert(); }
  catch (e) { $("#inst-zaehler").textContent = e.message; }
}
function zeichneInstalliert() {
  const tb = $("#tab-installiert tbody"); setze(tb, );
  if (!Z.installiert) return;
  const f = $("#f-filter-inst").value.trim().toLowerCase();
  const treffer = Z.installiert.filter((p) => !f || p.name.toLowerCase().includes(f) || p.beschreibung.toLowerCase().includes(f));
  for (const p of treffer.slice(0, 800)) {
    tb.append(el("tr", {}, el("td", {}, el("strong", {}, p.name)), el("td", { class: "mono" }, p.version),
      el("td", {}, p.arch), el("td", { class: "zahl" }, groesse(p.groesse_kb * 1024)), el("td", { class: "hinweis" }, p.beschreibung)));
  }
  $("#inst-zaehler").textContent = `${Z.installiert.length} Pakete installiert` +
    (f ? ` · ${treffer.length} Treffer` : "") + (treffer.length > 800 ? " · die ersten 800 angezeigt" : "");
}

// --------------------------------------------------------- Distributions-Upgrade
async function distLaden() {
  const kopf = $("#dist-kopf");
  setze(kopf, el("p", { class: "hinweis" }, "lade …"));
  try {
    const [d, i] = await Promise.all([api("/api/dist"), api("/api/info")]);
    Z.dist = d; Z.info = i;
    const teile = [];
    if (!d.moeglich) {
      teile.push(meldung("warn", el("p", {}, d.grund)));
      eigenAus("#btn-dist-sim", true);
    } else {
      eigenAus("#btn-dist-sim", false);
      teile.push(el("h2", {}, `${i.os}  →  Debian „${d.neu}“`));
      const frei = Number(d.frei_var_mb || 0);
      const offen = Z.updates ? Z.updates.aktualisiert.length : null;
      const zeile = (art, t, w) => el("tr", {}, el("td", {}, abz(art === "ok" ? "OK" : art === "warn" ? "Hinweis" : "Problem", art)),
        el("td", {}, el("strong", {}, t)), el("td", {}, w));
      teile.push(el("table", { class: "tabelle" }, el("tbody", {},
        zeile(i.dpkg_ok === "ja" ? "ok" : "fehler", "dpkg-Zustand", i.dpkg_ok === "ja" ? "sauber" : "unvollständige Installationen – zuerst reparieren"),
        zeile(frei >= 2048 ? "ok" : frei >= 1024 ? "warn" : "fehler", "Freier Platz in /var", groesse(frei * 1048576) + " (empfohlen ≥ 2 GiB)"),
        zeile(offen === null ? "warn" : offen === 0 ? "ok" : "warn", "Updates des aktuellen Release",
          offen === null ? "noch nicht geprüft (Reiter „Updates“)" : offen === 0 ? "alle eingespielt" : `${offen} offen – vorher einspielen empfohlen`),
        zeile(i.gehalten.trim() ? "warn" : "ok", "Gehaltene Pakete", i.gehalten.trim() || "keine"),
        zeile(i.neustart === "ja" ? "warn" : "ok", "Ausstehender Neustart", i.neustart === "ja" ? "vorher neu starten empfohlen" : "nein"),
        zeile(d.quellen_neu.length ? "ok" : "fehler", "Quellen, die umgestellt werden", d.quellen_neu.join(", ") || "keine – Codename nicht gefunden"),
        d.quellen_gleich.length ? zeile("warn", "Quellen ohne Änderung", d.quellen_gleich.join(", ")) : null)));
    }
    setze(kopf, ...teile);
    $("#btn-dist-zurueck").hidden = !i.dist_vorbereitet;
    $("#btn-neustart").hidden = i.neustart !== "ja";
    distStartFreigeben();
  } catch (e) {
    setze(kopf, meldung("fehler", el("p", {}, e.message)));
  }
}
function distBlockaden() {
  const s = Z.distSim, gruende = [];
  if (!s) { gruende.push("Erst die Simulation ausführen."); return gruende; }
  if (s.suite && s.suite !== "stable") gruende.push(`Das Ziel-Release ist derzeit „${s.suite}“ und kein stabiles Release.`);
  if (s.fehler.some((f) => /^(E:|Err:|W: Failed to fetch|W: Some index files failed)/.test(f)))
    gruende.push("Nicht alle Paketlisten des neuen Release ließen sich laden (siehe oben).");
  if (Z.info && Z.info.dpkg_ok === "nein") gruende.push("dpkg meldet unvollständige Installationen.");
  if (!s.aktualisiert.length) gruende.push("Die Simulation hat keine Aktualisierungen gefunden.");
  return gruende;
}
function distStartFreigeben() { eigenAus("#btn-dist-start", distBlockaden().length > 0); }
function zeichneDistErgebnis(r) {
  const box = $("#dist-ergebnis"), s = Z.distSim;
  if (!s) { setze(box, r && r.fehler ? meldung("fehler", el("p", {}, r.fehler)) : ""); distStartFreigeben(); return; }
  const blockaden = distBlockaden();
  setze(box, 
    el("h3", {}, "Ergebnis der Simulation"),
    el("div", { class: "zahlen" },
      el("div", {}, el("strong", {}, s.aktualisiert.length), "aktualisiert"),
      el("div", {}, el("strong", {}, s.neu.length), "neu installiert"),
      el("div", { class: s.entfernt.length ? "rot" : "" }, el("strong", {}, s.entfernt.length), "entfernt"),
      el("div", {}, el("strong", {}, groesse(s.bytes)), "Download")),
    s.gesperrt && s.gesperrt.length ? blockiertMeldung(s.gesperrt) : null,
    s.fehler.length ? meldung("fehler", el("p", {}, el("strong", {}, "Meldungen beim Laden der neuen Paketlisten:")),
      ...s.fehler.map((f) => el("p", { class: "mono" }, f)),
      el("p", { class: "hinweis" }, "Fremdquellen ohne neues Release unter „Einstellungen → Paketquellen“ deaktivieren und erneut simulieren.")) : null,
    blockaden.length ? meldung("warn", ...blockaden.map((g) => el("p", {}, g))) : meldung("ok", el("p", {}, "Bereit für das Distributions-Upgrade.")),
    s.entfernt.length ? el("details", { open: true }, el("summary", {}, `Wird ENTFERNT (${s.entfernt.length})`),
      el("ul", {}, ...s.entfernt.map((x) => el("li", {}, `${x.name} ${x.alt}`)))) : null,
    s.neu.length ? el("details", {}, el("summary", {}, `Wird neu installiert (${s.neu.length})`),
      el("ul", {}, ...s.neu.map((x) => el("li", {}, `${x.name} ${x.neu}`)))) : null,
    s.aktualisiert.length ? el("details", {}, el("summary", {}, `Wird aktualisiert (${s.aktualisiert.length})`),
      el("ul", {}, ...s.aktualisiert.map((x) => el("li", {}, `${x.name}: ${x.alt} → ${x.neu}`)))) : null);
  distStartFreigeben();
}
async function distSimulieren() {
  Z.distSim = null; zeichneDistErgebnis();
  await auftrag("/api/dist-simulieren", {}, async (r) => {
    Z.distSim = r.status === "ok" ? r.ergebnis : null;
    zeichneDistErgebnis(r);
  });
}
async function distStarten() {
  const s = Z.distSim; if (!s) return;
  const eingabe = el("input", { placeholder: "UPGRADE", spellcheck: "false" });
  const backup = el("input", { type: "checkbox" });
  const p = dialog("Distributions-Upgrade starten", [
    meldung("warn",
      el("p", {}, el("strong", {}, `${s.aktualisiert.length} Pakete werden aktualisiert, ${s.neu.length} neu installiert, ${s.entfernt.length} entfernt (${groesse(s.bytes)}).`)),
      el("p", {}, "Das dauert je nach System 10–60 Minuten. Dienste werden dabei neu gestartet."),
      el("p", {}, "Geänderte Konfigurationsdateien bleiben erhalten; neue Paketversionen davon landen als *.dpkg-dist daneben."),
      el("p", {}, "Die Paketquellen werden vorher gesichert. Scheitert das Herunterladen, werden sie automatisch zurückgesetzt.")),
    el("label", { class: "haken" }, backup, "Es gibt ein aktuelles Backup bzw. einen Snapshot des Zielsystems"),
    el("label", {}, "Zur Bestätigung UPGRADE eingeben", eingabe),
  ], [{ text: "Abbrechen", wert: null, class: "leise" }, { text: "Distributions-Upgrade starten", wert: "ok", class: "gefahr voll", aus: true }]);
  const pruefe = () => { dialog.knoepfe.ok.disabled = !(backup.checked && eingabe.value.trim() === "UPGRADE"); };
  backup.addEventListener("change", pruefe); eingabe.addEventListener("input", pruefe);
  if ((await p) !== "ok") return;
  await auftrag("/api/dist-ausfuehren", { bestaetigung: "UPGRADE" }, async (r) => {
    Z.distSim = null; Z.updates = null; zeichneUpdates();
    await infoLaden(); await distLaden();
    setze($("#dist-ergebnis"), r.status === "ok"
      ? meldung("ok", el("p", {}, el("strong", {}, "Distributions-Upgrade abgeschlossen.")), el("p", {}, "Bitte jetzt neu starten."))
      : meldung("fehler", el("p", {}, r.fehler || "Fehlgeschlagen – siehe Protokoll.")));
  });
}

// ---------------------------------------------------------------- Einstellungen
function zeichneHosts() {
  const hosts = (Z.z.profil && Z.z.profil.erlaubte_hosts) || [];
  setze($("#liste-hosts"), ...(hosts.length ? hosts.map((h) => el("li", {}, h,
    el("button", { title: "entfernen", onclick: async () => {
      try { await api("/api/erlauben", { host: h, entfernen: true }); await zustandLaden(); } catch (e) { fehlerZeigen(e); } } }, "×")))
    : [el("li", { class: "hinweis" }, "keine")]));
}
async function quellenZeigen() {
  const box = $("#quellen");
  try {
    const r = await api("/api/quellen");
    box.hidden = false; setze(box, );
    for (const block of r.text.split(/^### /m).filter(Boolean)) {
      const [pfad, ...rest] = block.split("\n");
      const name = pfad.split("/").pop();
      const kopf = el("div", { class: "knopfleiste" }, el("strong", {}, pfad));
      if (pfad.startsWith("/etc/apt/sources.list.d/")) {
        kopf.append(el("button", { class: "leise", onclick: async () => {
          if (!(await bestaetigen("Quelle deaktivieren", `${name} wird umbenannt und von apt nicht mehr gelesen.`, "Deaktivieren", "gefahr"))) return;
          try { await api("/api/quelle-deaktivieren", { datei: name }); quellenZeigen(); } catch (e) { fehlerZeigen(e); } } }, "deaktivieren"));
      }
      box.append(kopf, document.createTextNode(rest.join("\n") + "\n"));
    }
  } catch (e) { fehlerZeigen(e); }
}

// --------------------------------------------------------------------- Reiter
const GELADEN = new Set();
function zeigeReiter(name) {
  for (const b of document.querySelectorAll(".reiter button")) b.classList.toggle("aktiv", b.dataset.reiter === name);
  for (const d of document.querySelectorAll(".reiter-inhalt")) d.hidden = d.id !== "reiter-" + name;
  if (name === "installiert" && !Z.installiert) installiertLaden();
  if (name === "dist") distLaden();
  if (name === "einstellungen") zeichneHosts();
}

// ---------------------------------------------------------------------- Start
function verdrahten() {
  $("#form-ziel").addEventListener("submit", (e) => { e.preventDefault(); verbinden("erstzugang"); });
  $("#btn-wartung").addEventListener("click", () => verbinden("wartung"));
  $("#f-profil").addEventListener("change", (e) => { const p = Z.ziele[Number(e.target.value)]; if (p) profilZuForm(p); });
  $("#f-host").addEventListener("input", wartungKnopf);
  $("#f-port").addEventListener("input", wartungKnopf);
  $("#btn-trennen").addEventListener("click", async () => {
    try { Z.z = await api("/api/trennen", {}); } catch (e) { fehlerZeigen(e); }
    zeichneZustand(); profileLaden();
    const p = aktuellesProfil(); if (p) profilZuForm(p);
  });
  for (const b of document.querySelectorAll(".reiter button")) b.addEventListener("click", () => zeigeReiter(b.dataset.reiter));
  $("#btn-pruefen-vor").addEventListener("click", voraussetzungenPruefen);
  $("#btn-einrichten").addEventListener("click", einrichten);
  $("#btn-zu-wartung").addEventListener("click", async () => {
    const z = Z.z; $("#f-host").value = z.ziel; $("#f-port").value = z.port; await verbinden("wartung");
  });
  $("#btn-updates-pruefen").addEventListener("click", updatesPruefen);
  $("#f-filter-updates").addEventListener("input", zeichneUpdates);
  $("#btn-alle").addEventListener("click", () => { if (Z.updates) { Z.auswahl = new Set(Z.updates.aktualisiert.map((p) => p.name)); zeichneUpdates(); } });
  $("#btn-keine").addEventListener("click", () => { Z.auswahl.clear(); zeichneUpdates(); });
  $("#btn-sicherheit").addEventListener("click", () => { if (Z.updates) { Z.auswahl = new Set(Z.updates.aktualisiert.filter((p) => p.sicherheit).map((p) => p.name)); zeichneUpdates(); } });
  $("#btn-einspielen").addEventListener("click", einspielen);
  $("#btn-autoremove").addEventListener("click", () => simulierenUndBestaetigen("autoremove", [], "Nicht benötigte Pakete entfernen"));
  $("#btn-installieren").addEventListener("click", () => {
    const n = $("#f-neupaket").value.split(/[\s,]+/).filter(Boolean);
    if (n.length) simulierenUndBestaetigen("install", n, `Installieren: ${n.join(", ")}`);
  });
  $("#btn-installiert-laden").addEventListener("click", installiertLaden);
  $("#f-filter-inst").addEventListener("input", zeichneInstalliert);
  $("#btn-dist-laden").addEventListener("click", distLaden);
  $("#btn-dist-sim").addEventListener("click", distSimulieren);
  $("#btn-dist-start").addEventListener("click", distStarten);
  $("#btn-dist-zurueck").addEventListener("click", async () => {
    if (!(await bestaetigen("Quellen zurücksetzen", "Die Paketquellen werden aus der Sicherung wiederhergestellt.", "Zurücksetzen"))) return;
    try { const r = await api("/api/dist-zuruecksetzen", {}); await bestaetigen("Erledigt", r.text, "OK"); distLaden(); } catch (e) { fehlerZeigen(e); }
  });
  $("#btn-neustart").addEventListener("click", async () => {
    if (!(await bestaetigen("Neustart", `${Z.z.beschreibung} wird neu gestartet. Danach bitte neu verbinden.`, "Neu starten", "gefahr"))) return;
    try { const r = await api("/api/neustart", {}); await bestaetigen("Neustart", r.text, "OK"); } catch (e) { fehlerZeigen(e); }
  });
  $("#btn-host-add").addEventListener("click", async () => {
    const h = $("#f-neuhost").value.trim(); if (!h) return;
    try { await api("/api/erlauben", { host: h }); $("#f-neuhost").value = ""; await zustandLaden(); } catch (e) { fehlerZeigen(e); }
  });
  $("#btn-quellen").addEventListener("click", quellenZeigen);
  $("#btn-entfernen").addEventListener("click", async () => {
    if (!(await bestaetigen("Wartungszugang entfernen",
      "Benutzer, sudo-Regel, Hilfsskript und sshd-Freischaltung werden vom Ziel entfernt. Der lokale Schlüssel wird gelöscht.",
      "Entfernen", "gefahr"))) return;
    await auftrag("/api/entfernen", {}, async () => { await zustandLaden(); await profileLaden(); voraussetzungenPruefen(); infoLaden(); });
  });
  $("#btn-profil-loeschen").addEventListener("click", async () => {
    const z = Z.z;
    if (!(await bestaetigen("Gespeichertes Ziel löschen", `${z.ziel}:${z.port} wird aus der Liste entfernt (Wartungsschlüssel bleibt erhalten).`, "Löschen"))) return;
    try { await api("/api/ziel-loeschen", { host: z.ziel, port: z.port }); await profileLaden(); } catch (e) { fehlerZeigen(e); }
  });
}

(async function start() {
  verdrahten();
  try {
    await Promise.all([profileLaden(), schluesselLaden(), zustandLaden()]);
    if (Z.ziele.length && !Z.z.verbunden) { profilZuForm(Z.ziele[0]); $("#f-profil").value = "0"; }
    if (Z.z.verbunden) { infoLaden(); voraussetzungenPruefen(); }
    if (Z.z.auftrag && Z.z.auftrag.status === "laeuft") folgen(Z.z.auftrag).catch(fehlerZeigen);
  } catch (e) { fehlerZeigen(e); }
})();
