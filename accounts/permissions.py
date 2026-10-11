# Какие роли что могут. Чтобы изменить права, правьте только этот файл.
SERVICE_ROLES = {"owner", "admin", "waiter", "cashier"}  # зал и заказы
PAYMENT_ROLES = {"owner", "admin", "cashier"}            # оплата и чеки
KITCHEN_ROLES = {"owner", "admin", "cook"}               # экран кухни
REPORT_ROLES = {"owner", "admin"}                        # отчёты
SEE_ALL_ROLES = {"owner", "admin", "cashier"}            # видят заказы всех официантов
CANCEL_SENT_ROLES = {"owner", "admin"}                   # могут отменить заказ, уже ушедший на кухню
STOCK_ROLES = {"owner", "admin"}                         # склад: приход и пересчёт