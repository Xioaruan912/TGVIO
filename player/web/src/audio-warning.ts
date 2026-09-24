import { confirmAudioEnable } from "./ui";
import { prefs, setPref } from "./settings";

let warningShownThisOpen = false;
let frequencyChoiceOfferedThisOpen = false;
let promptInFlight = false;

export async function requestAudioEnable(host: HTMLElement): Promise<boolean> {
  if (prefs.soundPromptFrequency === "once-per-open" && warningShownThisOpen) {
    return true;
  }
  if (promptInFlight) return false;

  promptInFlight = true;
  const offerOncePerOpen =
    prefs.soundPromptFrequency === "every-time" && !frequencyChoiceOfferedThisOpen;
  try {
    const choice = await confirmAudioEnable(host, offerOncePerOpen);
    warningShownThisOpen = true;
    if (offerOncePerOpen) frequencyChoiceOfferedThisOpen = true;
    if (choice === "enable-once-per-open") {
      setPref("soundPromptFrequency", "once-per-open");
      return true;
    }
    return choice === "enable";
  } finally {
    promptInFlight = false;
  }
}
