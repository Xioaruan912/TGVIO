import "../../src/style.css";
import {buildLogin} from "../../src/ui";
// Layout fixture only; never consumes or sends credentials.
document.querySelector("#app")!.append(buildLogin(async () => undefined));
