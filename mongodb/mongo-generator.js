const numNewProducts = Math.floor(Math.random() * 8) + 3;
const numNewUsers = Math.floor(Math.random() * 15) + 5;
const numNewOrders = Math.floor(Math.random() * 40) + 40;

const chaosRate = (typeof CHAOS_RATE !== 'undefined' && CHAOS_RATE !== null)
  ? (Number(CHAOS_RATE) || 0)
  : 0;

let productsCreated = 0; let usersCreated = 0; let ordersCreated = 0;

for (let i = 0; i < numNewProducts; i++) {
  db.products.insertOne({
    name: 'Product_' + new Date().toISOString() + '_' + i,
    price: Math.floor(Math.random() * 495) * 10000 + 50000,
    stock: Math.floor(Math.random() * 100) + 50,
    category: ['Electronics', 'Fashion', 'Food', 'Home', 'Books'][Math.floor(Math.random() * 5)],
    created_at: new Date()
  });
  productsCreated++;
}

for (let i = 0; i < numNewUsers; i++) {
  db.users.insertOne({
    name: 'User_' + new Date().toISOString() + '_' + i,
    email: 'user_' + Date.now() + '_' + i + '@example.com',
    city: ['Jakarta', 'Surabaya', 'Bandung', 'Medan', 'Makassar'][Math.floor(Math.random() * 5)],
    created_at: new Date()
  });
  usersCreated++;
}

db.products.updateMany({ stock: { $lt: 20 } }, { $inc: { stock: 100 } });

const products = db.products.find().toArray();
const users = db.users.find().toArray();

if (products.length > 0 && users.length > 0) {
  for (let i = 0; i < numNewOrders; i++) {
    const prod = products[Math.floor(Math.random() * products.length)];
    const user = users[Math.floor(Math.random() * users.length)];
    const qty = Math.floor(Math.random() * 3) + 1;

    const stockResult = db.products.updateOne({ _id: prod._id, stock: { $gte: qty } }, { $inc: { stock: -qty } });
    if (stockResult.matchedCount === 0) continue;

    db.orders.insertOne({
      user_id: user._id,
      product_id: prod._id,
      quantity: qty,
      total_price: prod.price * qty,
      status: ['pending', 'processing', 'shipped', 'delivered'][Math.floor(Math.random() * 4)],
      created_at: new Date(),
      hash_signature: (() => {
        const sig = (user._id.toString() + prod._id.toString()).substring(0, 12);
        if (chaosRate > 0 && Math.random() < chaosRate) {
          // Logical corruption: signature no longer matches expected derivation
          return sig.split('').reverse().join('');
        }
        return sig;
      })() // For RTO integrity audit
    });
    ordersCreated++;
  }
}

// Output exactly what the bash script needs for the CSV
print(`${productsCreated},${usersCreated},${ordersCreated}`);