import { brand } from "./brand.js";

/* The shopper key (spec 7.3), in ONE place.
 *
 * Two halves:
 *   ref     - a generated per-browser id. Free, instant, works before the
 *             shopper has typed anything, and dies with the browser.
 *   contact - a phone or email the shopper types once. Portable: it is how
 *             a basket bought on a laptop is found again on a phone.
 *
 * Both live in localStorage, so they survive closing the tab and restarting
 * the browser. Neither is a credential and neither can move money.
 *
 * The storage NAMES are derived from brand.shopId here and from
 * /agent/config in the widget, and the two must agree exactly. They did not
 * once - the widget's fallback used the config name ("grocery") while this
 * used the shop id ("freshkart"), so one browser quietly held two identities
 * and a shopper lost their history by switching between them.
 */

const REF_KEY = `velcrow-shopper-${brand.shopId}`;
const CONTACT_KEY = `velcrow-contact-${brand.shopId}`;

function read(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return ""; // private mode: identity is simply absent, never an error
  }
}

function write(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* nothing to do; the key is a convenience, not a requirement */
  }
}

export const shopperKey = {
  /** The per-browser id, minted on first use. */
  ref() {
    let value = read(REF_KEY);
    if (!value) {
      value = `shp_${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
      write(REF_KEY, value);
    }
    return value;
  },

  /** The contact the shopper typed, as they typed it. Empty until they do. */
  contact() {
    return read(CONTACT_KEY);
  },

  /** Stop being known on this device. Order history is untouched - it stays
   *  attached to the contact and comes back if it is typed again. */
  forget() {
    try {
      localStorage.removeItem(CONTACT_KEY);
    } catch {
      /* nothing to forget */
    }
  },

  remember(contactText) {
    const text = (contactText || "").trim();
    if (text) write(CONTACT_KEY, text);
    return text;
  },
};
