import os
import sys

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lunyx - smoke test portal de carpetas")
        self.resize(700, 300)

        central = QWidget()
        layout = QVBoxLayout(central)
        self.status = QLabel(
            "Tocá 'Examinar...' y elegí una carpeta (ej. /home/tu_usuario/Descargas/test)."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        button = QPushButton("Examinar...")
        button.clicked.connect(self.pick_folder)
        layout.addWidget(button)
        self.setCentralWidget(central)

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Elegir carpeta")
        if not path:
            self.status.setText("Cancelado, sin carpeta elegida.")
            return
        try:
            entries = sorted(os.listdir(path))
        except OSError as exc:
            self.status.setText(f"Path devuelto: {path}\nERROR al listar: {exc}")
            return
        self.status.setText(
            f"Path devuelto por el diálogo: {path}\n"
            f"Contenido ({len(entries)} items): {entries}"
        )


def main() -> int:
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
