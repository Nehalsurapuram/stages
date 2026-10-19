package com.autoimmune.orders;

import java.math.BigDecimal;
import java.util.List;
import java.util.Optional;

import org.springframework.stereotype.Repository;

/** Fixed in-memory orders. No database. */
@Repository
public class OrderRepository {

    private final List<Order> orders = List.of(
            new Order(1, "alice", List.of(
                    new Item("Laptop", new BigDecimal("899.00"), 1),
                    new Item("Mouse", new BigDecimal("25.50"), 2))),
            new Order(2, "bob", List.of(
                    new Item("Monitor", new BigDecimal("300.00"), 3),
                    new Item("Keyboard", new BigDecimal("100.00"), 1))),
            new Order(3, "alice", List.of(
                    new Item("Phone", new BigDecimal("650.00"), 2),
                    new Item("Case", new BigDecimal("15.00"), 1))));

    public List<Order> findAll() {
        return orders;
    }

    public Optional<Order> findById(long id) {
        return orders.stream().filter(o -> o.id() == id).findFirst();
    }
}
