import os
import sqlite3

from flask import Flask, render_template, request, redirect, session, abort, url_for, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv

app = Flask(__name__)

# Load environment variables from .env
load_dotenv()

# Secure Flask secret key
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')

if not app.config['SECRET_KEY']:
    raise RuntimeError("SECRET_KEY is missing. Check your .env file.")

@app.get('/service-worker.js')
def service_worker():
    return app.send_static_file('service-worker.js')


# Database connection
def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


def generate_product_code(conn):
    row = conn.execute(
        "SELECT code FROM products WHERE code LIKE 'SM-%' ORDER BY id DESC"
    ).fetchone()
    number = 1
    if row:
        suffix = row['code'].replace('SM-', '', 1)
        if suffix.isdigit():
            number = int(suffix) + 1

    while conn.execute(
        'SELECT id FROM products WHERE code = ?',
        (f'SM-{number:06d}',),
    ).fetchone():
        number += 1

    return f'SM-{number:06d}'


# Create database tables
def create_table():
    conn = get_db_connection()

    # Products table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        )
    ''')

    product_columns = {
        row['name']
        for row in conn.execute('PRAGMA table_info(products)').fetchall()
    }
    if 'cost_price' not in product_columns:
        conn.execute(
            'ALTER TABLE products ADD COLUMN cost_price REAL NOT NULL DEFAULT 0'
        )

    # Stock history table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            movement_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    ''')

    # Users table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')

    # Sales table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_amount REAL NOT NULL,
            amount_paid REAL NOT NULL,
            change_amount REAL NOT NULL,
            payment_method TEXT NOT NULL,
            cashier TEXT,
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Individual products sold in each transaction
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            product_code TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            subtotal REAL NOT NULL,
            FOREIGN KEY (sale_id) REFERENCES sales(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    ''')

    # Suppliers table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_person TEXT,
            phone TEXT,
            email TEXT,
            address TEXT
        )
    ''')

    # Create first administrator if one doesn't exist
    admin = conn.execute(
        'SELECT id FROM users WHERE username = ?',
        ('admin',)
    ).fetchone()

    if admin is None:
        initial_admin_password = os.getenv('INITIAL_ADMIN_PASSWORD')

        if not initial_admin_password:
            raise RuntimeError(
                "INITIAL_ADMIN_PASSWORD is missing from your .env file."
            )

        hashed_password = generate_password_hash(initial_admin_password)

        conn.execute('''
            INSERT INTO users (username, password, role)
            VALUES (?, ?, ?)
        ''', ('admin', hashed_password, 'Administrator'))

    conn.commit()
    conn.close()

# Login protection
def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        return function(*args, **kwargs)

    return wrapper


def admin_required(function):
    @wraps(function)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get('role') != 'Administrator':
            abort(403)

        return function(*args, **kwargs)

    return wrapper


# Login page
@app.route('/login', methods=['GET', 'POST'])
def login():

    if 'user_id' in session:
        destination = 'pos' if session.get('role') == 'Cashier' else 'home'
        return redirect(url_for(destination))

    error = None

    if request.method == 'POST':

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        conn = get_db_connection()

        user = conn.execute(
            'SELECT * FROM users WHERE username = ?',
            (username,)
        ).fetchone()

        conn.close()

        if user and check_password_hash(user['password'], password):
            session.clear()

            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']

            if session['role'] == 'Cashier':
                return redirect(url_for('pos'))
            destination = 'pos' if session.get('role') == 'Cashier' else 'home'
            return redirect(url_for(destination))

        error = 'Incorrect username or password.'

    return render_template('login.html', error=error)


# Logout
@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))


# Products
@app.route('/products', methods=['GET', 'POST'])
@admin_required
def products():

    conn = get_db_connection()

    if request.method == 'POST':

        name = request.form['name']
        quantity = request.form['quantity']
        price = request.form['price']
        cost_price = request.form['cost_price']
        code = generate_product_code(conn)

        try:
            conn.execute('''
                INSERT INTO products (name, code, quantity, cost_price, price)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, code, quantity, cost_price, price))

            conn.commit()

        except sqlite3.IntegrityError:
            conn.close()
            return 'This product code already exists.'

        conn.close()
        return redirect(url_for('products'))

    product_list = conn.execute(
        'SELECT * FROM products'
    ).fetchall()

    conn.close()

    return render_template(
        'products.html',
        products=product_list
    )


# Edit product
@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@admin_required
def edit_product(id):

    conn = get_db_connection()

    if request.method == 'POST':

        conn.execute('''
            UPDATE products
            SET name = ?, quantity = ?, cost_price = ?, price = ?
            WHERE id = ?
        ''', (
            request.form['name'],
            request.form['quantity'],
            request.form['cost_price'],
            request.form['price'],
            id
        ))

        conn.commit()
        conn.close()

        return redirect(url_for('products'))

    product = conn.execute(
        'SELECT * FROM products WHERE id = ?',
        (id,)
    ).fetchone()

    conn.close()

    if product is None:
        abort(404)

    return render_template('edit.html', product=product)


# Delete product
@app.route('/delete/<int:id>', methods=['POST'])
@admin_required
def delete_product(id):

    conn = get_db_connection()

    conn.execute(
        'DELETE FROM products WHERE id = ?',
        (id,)
    )

    conn.commit()
    conn.close()

    return redirect(url_for('products'))


# Stock management
@app.route('/stock', methods=['GET', 'POST'])
@admin_required
def stock():

    conn = get_db_connection()

    if request.method == 'POST':

        product_id = request.form.get('product_id', type=int)
        quantity = request.form.get('quantity', type=int)

        if product_id is None or quantity is None or quantity < 1:
            conn.close()
            abort(400)

        product = conn.execute(
            'SELECT id FROM products WHERE id = ?',
            (product_id,)
        ).fetchone()

        if product is None:
            conn.close()
            abort(404)

        conn.execute(
            'UPDATE products SET quantity = quantity + ? WHERE id = ?',
            (quantity, product_id)
        )

        conn.execute('''
            INSERT INTO stock_history
            (product_id, movement_type, quantity)
            VALUES (?, 'IN', ?)
        ''', (product_id, quantity))

        conn.commit()
        conn.close()

        return redirect(url_for('stock'))

    product_list = conn.execute(
        'SELECT * FROM products'
    ).fetchall()

    conn.close()

    return render_template('stock.html', products=product_list)


@app.route('/pos')
@login_required
def pos():
    conn = get_db_connection()

    products = conn.execute('''
        SELECT * FROM products
        WHERE quantity > 0
        ORDER BY name ASC
    ''').fetchall()

    conn.close()

    return render_template('pos.html', products=products)


class SaleInputError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def parse_sale_items(items):
    if not isinstance(items, list) or not items:
        raise SaleInputError('A sale must contain at least one product.')

    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise SaleInputError('Invalid sale item.')

        product_id = item.get('id')
        quantity = item.get('quantity')
        if (
            type(product_id) is not int
            or product_id < 1
            or type(quantity) is not int
            or quantity < 1
        ):
            raise SaleInputError('Enter a valid product and quantity.')

        quantities[product_id] = quantities.get(product_id, 0) + quantity

    return quantities


def create_sale(conn, quantities, payment_method, cashier):
    if payment_method not in {'Cash', 'Mobile Money', 'Card'}:
        raise SaleInputError('Select a valid payment method.')

    with conn:
        conn.execute('BEGIN IMMEDIATE')
        sale_items = []
        total_cents = 0

        for product_id, quantity in quantities.items():
            product = conn.execute(
                'SELECT * FROM products WHERE id = ?', (product_id,)
            ).fetchone()
            if product is None:
                raise SaleInputError('A product in the sale no longer exists.', 404)
            if quantity > product['quantity']:
                raise SaleInputError(
                    f"Not enough stock for {product['name']}.", 409
                )

            price_cents = int(
                (Decimal(str(product['price'])) * 100).quantize(
                    Decimal('1'), rounding=ROUND_HALF_UP
                )
            )
            subtotal_cents = price_cents * quantity
            total_cents += subtotal_cents
            sale_items.append((product, quantity, price_cents, subtotal_cents))

        total_amount = total_cents / 100
        cursor = conn.execute(
            '''
            INSERT INTO sales
                (total_amount, amount_paid, change_amount, payment_method, cashier)
            VALUES (?, ?, 0, ?, ?)
            ''',
            (total_amount, total_amount, payment_method, cashier),
        )
        sale_id = cursor.lastrowid

        for product, quantity, price_cents, subtotal_cents in sale_items:
            conn.execute(
                'UPDATE products SET quantity = quantity - ? WHERE id = ?',
                (quantity, product['id']),
            )
            conn.execute(
                '''
                INSERT INTO sale_items
                    (sale_id, product_id, product_name, product_code,
                     quantity, unit_price, subtotal)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    sale_id,
                    product['id'],
                    product['name'],
                    product['code'],
                    quantity,
                    price_cents / 100,
                    subtotal_cents / 100,
                ),
            )
            conn.execute(
                '''
                INSERT INTO stock_history
                    (product_id, movement_type, quantity)
                VALUES (?, 'OUT', ?)
                ''',
                (product['id'], quantity),
            )

    return sale_id, total_amount


@app.post('/complete-sale')
@login_required
def complete_sale():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, message='Invalid checkout request.'), 400

    try:
        quantities = parse_sale_items(payload.get('cart'))
        conn = get_db_connection()
        try:
            sale_id, total_amount = create_sale(
                conn,
                quantities,
                payload.get('payment_method'),
                session.get('username'),
            )
        finally:
            conn.close()
    except SaleInputError as error:
        return jsonify(success=False, message=str(error)), error.status_code

    return jsonify(success=True, sale_id=sale_id, total=total_amount)


def update_sale(conn, sale_id, quantities, payment_method):
    if payment_method not in {'Cash', 'Mobile Money', 'Card'}:
        raise SaleInputError('Select a valid payment method.')

    with conn:
        conn.execute('BEGIN IMMEDIATE')
        sale = conn.execute(
            'SELECT id FROM sales WHERE id = ?', (sale_id,)
        ).fetchone()
        if sale is None:
            raise SaleInputError('Sale not found.', 404)

        old_rows = conn.execute(
            '''
            SELECT product_id, SUM(quantity) AS quantity
            FROM sale_items
            WHERE sale_id = ?
            GROUP BY product_id
            ''',
            (sale_id,),
        ).fetchall()
        old_quantities = {row['product_id']: row['quantity'] for row in old_rows}

        products = {}
        sale_items = []
        total_cents = 0
        for product_id, quantity in quantities.items():
            product = conn.execute(
                'SELECT * FROM products WHERE id = ?', (product_id,)
            ).fetchone()
            if product is None:
                raise SaleInputError('A selected product no longer exists.', 404)

            products[product_id] = product
            price_cents = int(
                (Decimal(str(product['price'])) * 100).quantize(
                    Decimal('1'), rounding=ROUND_HALF_UP
                )
            )
            subtotal_cents = price_cents * quantity
            total_cents += subtotal_cents
            sale_items.append((product, quantity, price_cents, subtotal_cents))

        for product_id in old_quantities.keys() | quantities.keys():
            product = products.get(product_id)
            if product is None:
                product = conn.execute(
                    'SELECT * FROM products WHERE id = ?', (product_id,)
                ).fetchone()
            difference = quantities.get(product_id, 0) - old_quantities.get(product_id, 0)

            if difference > 0 and product is None:
                raise SaleInputError('A product being added no longer exists.', 404)
            if product is None:
                continue
            if difference > 0 and difference > product['quantity']:
                raise SaleInputError(
                    f"Not enough stock for {product['name']}.", 409
                )
            if difference:
                conn.execute(
                    'UPDATE products SET quantity = quantity - ? WHERE id = ?',
                    (difference, product_id),
                )
                movement = 'OUT' if difference > 0 else 'IN'
                conn.execute(
                    '''
                    INSERT INTO stock_history
                        (product_id, movement_type, quantity)
                    VALUES (?, ?, ?)
                    ''',
                    (product_id, movement, abs(difference)),
                )

        conn.execute('DELETE FROM sale_items WHERE sale_id = ?', (sale_id,))
        for product, quantity, price_cents, subtotal_cents in sale_items:
            conn.execute(
                '''
                INSERT INTO sale_items
                    (sale_id, product_id, product_name, product_code,
                     quantity, unit_price, subtotal)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    sale_id,
                    product['id'],
                    product['name'],
                    product['code'],
                    quantity,
                    price_cents / 100,
                    subtotal_cents / 100,
                ),
            )

        total_amount = total_cents / 100
        conn.execute(
            '''
            UPDATE sales
            SET total_amount = ?, amount_paid = ?, change_amount = 0,
                payment_method = ?
            WHERE id = ?
            ''',
            (total_amount, total_amount, payment_method, sale_id),
        )

    return total_amount


def parse_sale_form(form):
    product_ids = form.getlist('product_id')
    quantities = form.getlist('quantity')
    if not product_ids or len(product_ids) != len(quantities):
        raise SaleInputError('Add at least one complete product line.')

    items = []
    try:
        for product_id, quantity in zip(product_ids, quantities):
            items.append({'id': int(product_id), 'quantity': int(quantity)})
    except ValueError as error:
        raise SaleInputError('Enter a valid product and quantity.') from error

    return parse_sale_items(items)


@app.route('/sales', methods=['GET', 'POST'])
@admin_required
def sales():
    if request.method == 'POST':
        try:
            quantities = parse_sale_form(request.form)
            conn = get_db_connection()
            try:
                sale_id, total_amount = create_sale(
                    conn,
                    quantities,
                    request.form.get('payment_method'),
                    session.get('username'),
                )
            finally:
                conn.close()
            flash(f'Sale #{sale_id} entered for GH₵ {total_amount:.2f}.', 'success')
        except SaleInputError as error:
            flash(str(error), 'error')
        return redirect(url_for('sales'))

    conn = get_db_connection()
    try:
        sale_list = conn.execute('''
            SELECT sales.id, sales.total_amount, sales.amount_paid,
                   sales.change_amount, sales.payment_method, sales.cashier,
                   sales.sale_date,
                   GROUP_CONCAT(
                       sale_items.product_name || ' x ' || sale_items.quantity,
                       ', '
                   ) AS items
            FROM sales
            LEFT JOIN sale_items ON sale_items.sale_id = sales.id
            GROUP BY sales.id
            ORDER BY sales.sale_date DESC, sales.id DESC
        ''').fetchall()
        product_list = conn.execute(
            'SELECT id, name, code, quantity, price FROM products WHERE quantity > 0 ORDER BY name'
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        'sales.html', sales=sale_list, products=product_list
    )


@app.route('/sales/<int:sale_id>')
@admin_required
def sale_detail(sale_id):
    conn = get_db_connection()
    try:
        sale = conn.execute(
            'SELECT * FROM sales WHERE id = ?', (sale_id,)
        ).fetchone()
        items = conn.execute(
            'SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id',
            (sale_id,),
        ).fetchall()
    finally:
        conn.close()

    if sale is None:
        abort(404)
    return render_template('sale_detail.html', sale=sale, items=items)


@app.route('/sales/<int:sale_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_sale(sale_id):
    if request.method == 'POST':
        try:
            quantities = parse_sale_form(request.form)
            conn = get_db_connection()
            try:
                total_amount = update_sale(
                    conn,
                    sale_id,
                    quantities,
                    request.form.get('payment_method'),
                )
            finally:
                conn.close()
            flash(f'Sale updated. New total: GH₵ {total_amount:.2f}.', 'success')
            return redirect(url_for('sale_detail', sale_id=sale_id))
        except SaleInputError as error:
            flash(str(error), 'error')
            return redirect(url_for('edit_sale', sale_id=sale_id))

    conn = get_db_connection()
    try:
        sale = conn.execute(
            'SELECT * FROM sales WHERE id = ?', (sale_id,)
        ).fetchone()
        if sale is None:
            abort(404)
        items = conn.execute(
            'SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id',
            (sale_id,),
        ).fetchall()
        product_list = conn.execute(
            'SELECT id, name, code, quantity, price FROM products ORDER BY name'
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        'edit_sale.html', sale=sale, items=items, products=product_list
    )


@app.post('/sales/<int:sale_id>/delete')
@admin_required
def delete_sale(sale_id):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            sale = conn.execute(
                'SELECT id FROM sales WHERE id = ?', (sale_id,)
            ).fetchone()
            if sale is None:
                abort(404)
            conn.execute('DELETE FROM sale_items WHERE sale_id = ?', (sale_id,))
            conn.execute('DELETE FROM sales WHERE id = ?', (sale_id,))
    finally:
        conn.close()

    flash('Sale deleted. Inventory was not restored.', 'success')
    return redirect(url_for('sales'))


@app.route('/suppliers', methods=['GET', 'POST'])
@admin_required
def suppliers():
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                flash('Supplier name is required.', 'error')
            else:
                conn.execute(
                    '''
                    INSERT INTO suppliers
                        (name, contact_person, phone, email, address)
                    VALUES (?, ?, ?, ?, ?)
                    ''',
                    (
                        name,
                        request.form.get('contact_person', '').strip(),
                        request.form.get('phone', '').strip(),
                        request.form.get('email', '').strip(),
                        request.form.get('address', '').strip(),
                    ),
                )
                conn.commit()
                flash('Supplier added successfully.', 'success')
                return redirect(url_for('suppliers'))

        supplier_list = conn.execute(
            'SELECT * FROM suppliers ORDER BY name ASC'
        ).fetchall()
        return render_template('suppliers.html', suppliers=supplier_list)
    finally:
        conn.close()


@app.post('/suppliers/<int:supplier_id>/delete')
@admin_required
def delete_supplier(supplier_id):
    conn = get_db_connection()
    try:
        with conn:
            result = conn.execute(
                'DELETE FROM suppliers WHERE id = ?', (supplier_id,)
            )
            if result.rowcount == 0:
                abort(404)
    finally:
        conn.close()

    flash('Supplier deleted.', 'success')
    return redirect(url_for('suppliers'))


@app.route('/reports')
@admin_required
def reports():
    conn = get_db_connection()
    try:
        summary = conn.execute(
            '''
            SELECT
                COALESCE(COUNT(*), 0) AS sales_count,
                COALESCE(SUM(total_amount), 0) AS total_sales,
                COALESCE(SUM(amount_paid), 0) AS amount_paid,
                COALESCE(SUM(change_amount), 0) AS change_given
            FROM sales
            '''
        ).fetchone()

        payment_totals = conn.execute(
            '''
            SELECT payment_method,
                   COUNT(*) AS sales_count,
                   COALESCE(SUM(total_amount), 0) AS total_amount
            FROM sales
            GROUP BY payment_method
            ORDER BY total_amount DESC
            '''
        ).fetchall()

        top_products = conn.execute(
            '''
            SELECT product_name, product_code,
                   SUM(quantity) AS quantity_sold,
                   SUM(subtotal) AS revenue
            FROM sale_items
            GROUP BY product_id, product_name, product_code
            ORDER BY quantity_sold DESC
            LIMIT 10
            '''
        ).fetchall()

        low_stock = conn.execute(
            '''
            SELECT name, code, quantity
            FROM products
            WHERE quantity <= 5
            ORDER BY quantity ASC, name ASC
            '''
        ).fetchall()

        recent_sales = conn.execute(
            '''
            SELECT id, total_amount, payment_method, cashier, sale_date
            FROM sales
            ORDER BY sale_date DESC, id DESC
            LIMIT 20
            '''
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        'reports.html',
        summary=summary,
        payment_totals=payment_totals,
        top_products=top_products,
        low_stock=low_stock,
        recent_sales=recent_sales,
    )


@app.route('/users', methods=['GET', 'POST'])
@admin_required
def users():
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            role = request.form.get('role', '')

            if len(username) < 3:
                return 'Username must contain at least 3 characters.'
            if role not in {'Administrator', 'Cashier'}:
                return 'Invalid user role.'
            if len(password) < 8:
                return 'Password must contain at least 8 characters.'

            conn.execute(
                '''
                INSERT INTO users (username, password, role)
                VALUES (?, ?, ?)
                ''',
                (username, generate_password_hash(password), role),
            )
            conn.commit()
            return redirect(url_for('users'))

        user_list = conn.execute(
            'SELECT id, username, role FROM users ORDER BY id DESC'
        ).fetchall()
        return render_template('users.html', users=user_list)
    except sqlite3.IntegrityError:
        return 'This username already exists.'
    finally:
        conn.close()
# Dashboard
@app.route('/')
@admin_required
def home():
    conn = get_db_connection()

    try:
        total_products = conn.execute(
            'SELECT COUNT(*) FROM products'
        ).fetchone()[0]

        total_stock = conn.execute(
            'SELECT COALESCE(SUM(quantity), 0) FROM products'
        ).fetchone()[0]

        today_sales = conn.execute("""
            SELECT COALESCE(SUM(total_amount), 0)
            FROM sales
            WHERE DATE(sale_date) = DATE('now', 'localtime')
        """).fetchone()[0]

        low_stock = conn.execute("""
            SELECT COUNT(*)
            FROM products
            WHERE quantity <= 5
        """).fetchone()[0]

        return render_template(
            'dashboard.html',
            total_products=total_products,
            total_stock=total_stock,
            today_sales=today_sales,
            low_stock=low_stock
        )

    finally:
        conn.close()

# Initialize database when the application starts
create_table()

# Start application locally
if __name__ == '__main__':
    app.run(debug=False)
    