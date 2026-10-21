{
  description = "Dev environment for frontline-extractor";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          pkgs.chromium
          pkgs.chromedriver
        ];

        # main.py's webdriver.Chrome() otherwise falls back to Selenium
        # Manager's downloaded Chrome-for-Testing/chromedriver, which can't
        # find its shared libraries on NixOS.
        SE_BROWSER_PATH = "${pkgs.chromium}/bin/chromium";
        SE_CHROMEDRIVER = "${pkgs.chromedriver}/bin/chromedriver";
      };
    };
}
