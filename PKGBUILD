# Local package: build with `makepkg -si` from this directory.
pkgname=gpro-control
pkgver=0.8.0
pkgrel=1
pkgdesc="Battery status and DPI control for Logitech G PRO wireless mice"
arch=('any')
license=('MIT')
depends=('python' 'python-gobject' 'gtk4' 'librsvg')
optdepends=('xdg-desktop-portal: tray icon follows light/dark mode'
            'notification-daemon: alert popups on bare window managers (or mako, dunst, swaync)'
            'gnome-shell-extension-appindicator: tray icon on GNOME')
install=gpro-control.install
source=()

package() {
    cd "$startdir"
    install -Dm644 gpro_control/*.py -t "$pkgdir/usr/lib/gpro-control/gpro_control/"
    install -Dm755 gpro-control "$pkgdir/usr/bin/gpro-control"
    install -Dm644 60-gpro-control.rules "$pkgdir/usr/lib/udev/rules.d/60-gpro-control.rules"
    install -Dm644 gpro-control-monitor.service -t "$pkgdir/usr/lib/systemd/user/"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 io.github.gpro_control.svg -t "$pkgdir/usr/share/icons/hicolor/scalable/apps/"
    install -Dm644 io.github.gpro_control.desktop -t "$pkgdir/usr/share/applications/"
}
