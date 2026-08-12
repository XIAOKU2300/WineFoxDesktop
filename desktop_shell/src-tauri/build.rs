fn main() {
    println!("cargo:rerun-if-env-changed=WINE_FOX_UPDATE_FEED_URL");
    println!("cargo:rerun-if-env-changed=WINE_FOX_UPDATE_PUBKEY");
    tauri_build::build()
}
