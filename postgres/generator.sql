DO $$
DECLARE
    num_new_customers INT;
    num_new_products INT;
    num_new_orders INT;
    cust_count INT;
    prod_count INT;
    i INT;
    v_order_id INT;
    payment_methods TEXT[] := ARRAY['GoPay','OVO','DANA','LinkAja','Credit Card','QRIS'];
    chaos_rate DOUBLE PRECISION := 0;
BEGIN
    BEGIN
        chaos_rate := COALESCE(NULLIF(current_setting('drsim.chaos_rate', true), ''), '0')::DOUBLE PRECISION;
    EXCEPTION WHEN OTHERS THEN
        chaos_rate := 0;
    END;

    -- Ranges tuned for steady stream: products 5-10, customers 10-20, orders 40-80
    num_new_products := (RANDOM() * 6)::INT + 5;  -- 5..10
    num_new_customers := (RANDOM() * 11)::INT + 10; -- 10..20
    num_new_orders := (RANDOM() * 41)::INT + 40;  -- 40..80

    FOR i IN 1..num_new_products LOOP
        INSERT INTO products (name, price, category, stock)
        VALUES (
            'Product_' || TO_CHAR(NOW(), 'YYYYMMDD_HH24MISS') || '_' || i,
            (RANDOM() * 4950000 + 50000)::NUMERIC(10,2),
            (ARRAY['Elektronik','Fashion','Food','Home','Sports'])[(1 + FLOOR(RANDOM() * 5))::INT],
            (RANDOM() * 100)::INT + 50
        );
    END LOOP;

    FOR i IN 1..num_new_customers LOOP
        INSERT INTO customers (name, email, address)
        VALUES (
            'Customer_' || TO_CHAR(NOW(), 'YYYYMMDD_HH24MISS') || '_' || i,
            'cust_' || TO_CHAR(NOW(), 'HH24MISS') || '_' || i || '@example.com',
            (ARRAY['Jakarta','Surabaya','Bandung','Medan','Makassar'])[(1 + FLOOR(RANDOM() * 5))::INT]
        );
    END LOOP;

    cust_count := (SELECT COUNT(*) FROM customers);
    prod_count := (SELECT COUNT(*) FROM products);

    IF cust_count > 0 AND prod_count > 0 THEN
        FOR i IN 1..num_new_orders LOOP
            -- Insert order and capture its id, then store integrity hash that includes order id + timestamp
            INSERT INTO orders (user_id, amount)
            VALUES (
                (FLOOR(RANDOM() * cust_count) + 1)::INT,
                (RANDOM() * 5000000 + 50000)::NUMERIC(10,2)
            ) RETURNING id INTO v_order_id;

            UPDATE orders
            SET integrity_hash = CASE
                WHEN chaos_rate > 0 AND RANDOM() < chaos_rate THEN
                    md5('CORRUPT|' || v_order_id::text || '|' || (SELECT created_at::text FROM orders WHERE id = v_order_id))
                ELSE
                    md5(v_order_id::text || '|' || (SELECT created_at::text FROM orders WHERE id = v_order_id))
            END
            WHERE id = v_order_id;

            IF RANDOM() < 0.8 THEN
                INSERT INTO payments (order_id, amount, payment_method)
                SELECT
                    v_order_id,
                    (SELECT amount FROM orders WHERE id = v_order_id),
                    payment_methods[(1 + FLOOR(RANDOM() * 6))::INT];
            END IF;
        END LOOP;
    END IF;

    -- Emit a clearly-parseable NOTICE for monitoring: CUSTOM_STATS:customers,products,orders
    -- In PL/pgSQL RAISE formatting, placeholders are '%' (not '%s').
    RAISE NOTICE 'CUSTOM_STATS:%,%,%', num_new_customers, num_new_products, num_new_orders;
END $$;